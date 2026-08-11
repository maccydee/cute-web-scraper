"""Layered scraping engine: httpx, TLS impersonation, Playwright, stealth browser."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from .cache import PageCache
from .config import Config
from .converter import html_to_markdown, page_title
from .rate_limiter import DomainRateLimiter

if TYPE_CHECKING:  # playwright is heavy; keep it out of module import
    from playwright.async_api import Browser, Playwright

log = logging.getLogger(__name__)

_TIMEOUT_S = 30.0
_BLOCK_STATUSES = {403, 429, 503}
_USER_AGENT = "cute-web-scraper/0.1 (+https://github.com/maccydee/cute-web-scraper)"

_CONTENT_MIN_HTML = 10_000
_CONTENT_MIN_TEXT = 2_000
_CONTENT_MIN_LINKS = 10
"""What counts as a real page when the status code says otherwise. Every genuine
block observed was under 2KB (ASOS 296 bytes, eBay 1,832, Trustpilot 991), so these
thresholds sit well clear of them."""

_STEALTH_SETTLE_MS = 4000
"""How long a stealth render waits for client-side content to populate."""

_IMPERSONATE_PROFILES = ("chrome131", "safari17_0")
"""Tried in order when a plain request is blocked.

Not one magic value: ASOS clears with the Chrome fingerprint while eBay rejects
Chrome and accepts Safari, so the fallback has to try more than one.
"""

_CHALLENGE_SIGNATURES = (
    # Cloudflare
    "cf-browser-verification",
    "/cdn-cgi/challenge-platform",
    "__cf_chl",
    "<title>Just a moment",
    "Attention Required! | Cloudflare",
    # Akamai. eBay serves this with HTTP 200, so status alone never catches it —
    # without these the scraper accepts a 13KB interstitial as if it were the page.
    "Pardon our interruption",
    "Checking your browser before you access",
    "Checking your browser before accessing",
)
"""Every entry is specific enough that ordinary prose cannot trigger it. Matching the
bare phrase 'Just a moment' would silently blank real pages."""


@dataclass
class FetchResult:
    url: str
    html: str
    """Raw HTML. Required by the extractors -- markdown has no <a href> tags."""
    markdown: str
    status_code: int
    title: str
    links_count: int
    content_type: str
    blocked: bool
    block_reason: str | None
    via: str = "httpx"
    """Which tier fetched this: httpx, playwright, or impersonate:<profile>."""
    from_cache: bool = False


class Scraper:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._rate_limiter = DomainRateLimiter(config)
        self._semaphore = asyncio.Semaphore(config.max_concurrent)
        self._cache: PageCache[FetchResult] = PageCache(
            ttl_s=config.cache_ttl_s, max_entries=config.cache_max_entries
        )
        self._client: httpx.AsyncClient | None = None
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._browser_lock = asyncio.Lock()
        self._impersonate_sessions: dict[str, Any] = {}
        self._warmed: set[tuple[str, str]] = set()
        self._stealth_browser: Browser | None = None
        self._stealth_cm: Any = None
        self._stealth_lock = asyncio.Lock()

    async def __aenter__(self) -> Scraper:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(_TIMEOUT_S),
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        if self._stealth_browser is not None:
            await self._stealth_browser.close()
            self._stealth_browser = None
        if self._stealth_cm is not None:
            await self._stealth_cm.__aexit__(None, None, None)
            self._stealth_cm = None
        for session in self._impersonate_sessions.values():
            try:
                await session.close()
            except Exception:  # noqa: BLE001 - closing must not mask a real error
                log.debug("Failed to close an impersonation session", exc_info=True)
        self._impersonate_sessions.clear()
        self._warmed.clear()

    @property
    def http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("Scraper used outside its async context manager")
        return self._client

    @property
    def rate_limiter(self) -> DomainRateLimiter:
        return self._rate_limiter

    async def fetch(self, url: str, *, js_render: bool = False) -> FetchResult:
        _validate_url(url)
        cached = self._cache.get(url, js_render)
        if cached is not None:
            return replace(cached, from_cache=True)

        if js_render:
            html, status, content_type, via = await self._fetch_rendered_with_fallback(url)
        else:
            html, status, content_type, via = await self._fetch_static_escalating(url)

        result = self._build_result(url, html, status, content_type, via)
        self._cache.put(url, js_render, result)
        return result

    async def _fetch_rendered_with_fallback(self, url: str) -> tuple[str, int, str, str]:
        """Render in a browser, but fall back to TLS impersonation if that is blocked.

        Playwright and curl_cffi fail in opposite directions. Playwright runs
        JavaScript but is a *detectably automated* browser, so ASOS answers it with
        403 while letting curl_cffi straight through. Without this fallback, asking
        for js_render on ASOS returned 296 bytes where the default returned 613KB —
        requesting more capability got you less.
        """
        try:
            html, status, content_type = await self._fetch_rendered(url)
        except Exception as exc:  # noqa: BLE001
            if not self._config.impersonate:
                raise
            log.info("Rendering failed for %s (%s); falling back", url, type(exc).__name__)
            return await self._fetch_static_escalating(url)

        blocked, _ = _detect_block(status, html)
        if not blocked or not self._config.impersonate:
            return html, status, content_type, "playwright"

        log.info("The browser was blocked on %s; falling back to TLS impersonation", url)
        fallback = await self._fetch_static_escalating(url)
        if not _detect_block(fallback[1], fallback[0])[0]:
            return fallback

        last_resort = await self._try_stealth(url)
        if last_resort is not None:
            return last_resort
        # Everything blocked: keep the browser's answer, the fuller attempt.
        return html, status, content_type, "playwright"

    async def _try_stealth(self, url: str) -> tuple[str, int, str, str] | None:
        """Last resort: a browser with its automation tells patched out.

        Trustpilot defeats every other tier — it needs JavaScript (so curl_cffi
        cannot help) and detects ordinary Playwright. A stealth-patched browser
        gets the real page, and notably serves it under a 403, which is why block
        detection has to weigh content rather than status alone.
        """
        if not self._config.stealth:
            return None
        try:
            html, status, content_type = await self._fetch_stealth(url)
        except Exception as exc:  # noqa: BLE001 - last resort, never fatal
            log.info("Stealth rendering failed for %s: %s", url, _brief(exc))
            return None
        if _detect_block(status, html)[0]:
            log.info("Stealth rendering was blocked too on %s", url)
            return None
        log.info("Cleared %s with stealth rendering", url)
        return html, status, content_type, "stealth"

    async def _fetch_static_escalating(self, url: str) -> tuple[str, int, str, str]:
        """Plain request first; on a block, retry with browser TLS fingerprints.

        Some sites (ASOS, eBay) do not inspect the User-Agent at all — they
        fingerprint the TLS handshake itself, which no header or headless-browser
        flag can change. Escalating through real browser fingerprints clears that,
        and costs nothing on the ~95% of sites that never block in the first place.
        """
        try:
            html, status, content_type = await self._fetch_static(url)
        except httpx.HTTPError as exc:
            # A refusal is not always an HTTP status. ASOS drops the connection
            # outright rather than answering 403, so a transport error is itself a
            # signal to escalate rather than a reason to give up.
            if not self._config.impersonate:
                raise
            log.info("%s on %s; escalating to browser TLS fingerprints", type(exc).__name__, url)
            escalated = await self._escalate(url, fallback=("", 0, ""), after_error=True)
            if escalated[1] == 0:
                # Nothing answered at all. Returning an empty body here would look
                # like a successful blank page, so surface the original failure.
                raise
            return escalated

        if not self._config.impersonate:
            return html, status, content_type, "httpx"
        blocked, _ = _detect_block(status, html)
        if not blocked:
            return html, status, content_type, "httpx"
        return await self._escalate(url, fallback=(html, status, content_type))

    async def _escalate(
        self, url: str, *, fallback: tuple[str, int, str], after_error: bool = False
    ) -> tuple[str, int, str, str]:
        html, status, content_type = fallback
        reason = "connection refused" if after_error else "blocked"

        for profile in _IMPERSONATE_PROFILES:
            log.info("%s on %s; trying the %s TLS fingerprint", reason.capitalize(), url, profile)
            try:
                retry = await self._fetch_impersonated(url, profile)
            except Exception as exc:  # noqa: BLE001 - fall through to the next profile
                log.debug("Impersonation with %s failed for %s: %s", profile, url, exc)
                continue
            retry_html, retry_status, retry_type = retry
            still_blocked, _ = _detect_block(retry_status, retry_html)
            if not still_blocked:
                log.info("Cleared the block on %s using %s", url, profile)
                return retry_html, retry_status, retry_type, f"impersonate:{profile}"
            html, status, content_type = retry_html, retry_status, retry_type

        last_resort = await self._try_stealth(url)
        if last_resort is not None:
            return last_resort
        return html, status, content_type, "impersonate:exhausted"

    async def _fetch_static(self, url: str) -> tuple[str, int, str]:
        await self._rate_limiter.wait(url)
        async with self._semaphore:
            log.debug("GET %s", url)
            response = await self.http.get(url)
        content_type = response.headers.get("content-type", "")
        # Decoding a PDF or image as text produces garbage; skip the body entirely.
        body = response.text if _is_texty(content_type) else ""
        return body, response.status_code, content_type

    async def _impersonate_session(self, profile: str) -> Any:
        """One long-lived curl_cffi session per profile, so cookies persist."""
        from curl_cffi.requests import AsyncSession

        session = self._impersonate_sessions.get(profile)
        if session is None:
            session = AsyncSession()
            self._impersonate_sessions[profile] = session
        return session

    async def _warm(self, session: Any, url: str, profile: str) -> None:
        """Visit a site's origin once before its inner pages.

        A cold request often gets a stub: eBay's search page came back as 13KB of
        interstitial on a fresh session but 2.1MB of results once the session had
        picked up the cookies the homepage sets. Browsers get this for free by
        virtue of having been there before.
        """
        origin = _origin_of(url)
        key = (profile, origin)
        if key in self._warmed:
            return
        self._warmed.add(key)
        try:
            await self._rate_limiter.wait(origin)
            await session.get(origin, impersonate=profile, timeout=_TIMEOUT_S, allow_redirects=True)
            log.debug("Warmed %s session for %s", profile, origin)
        except Exception as exc:  # noqa: BLE001 - warming is best-effort
            log.debug("Could not warm %s for %s: %s", profile, origin, exc)

    async def _fetch_impersonated(self, url: str, profile: str) -> tuple[str, int, str]:
        """Fetch using a real browser's TLS fingerprint via curl_cffi."""
        session = await self._impersonate_session(profile)
        await self._warm(session, url, profile)

        await self._rate_limiter.wait(url)
        async with self._semaphore:
            response = await session.get(
                url,
                impersonate=profile,
                timeout=_TIMEOUT_S,
                # curl_cffi does not follow redirects by default, unlike the httpx
                # client above.
                allow_redirects=True,
            )
        content_type = str(response.headers.get("content-type", ""))
        body = response.text if _is_texty(content_type) else ""
        return body, int(response.status_code), content_type

    async def _ensure_stealth_browser(self) -> Browser:
        async with self._stealth_lock:
            if self._stealth_browser is not None:
                return self._stealth_browser
            from playwright.async_api import async_playwright
            from playwright_stealth import Stealth

            cm = Stealth().use_async(async_playwright())
            playwright = await cm.__aenter__()
            try:
                browser = await playwright.chromium.launch(
                    headless=True, args=["--disable-blink-features=AutomationControlled"]
                )
            except Exception:
                await cm.__aexit__(None, None, None)
                raise
            self._stealth_cm = cm
            self._stealth_browser = browser
            return self._stealth_browser

    async def _fetch_stealth(self, url: str) -> tuple[str, int, str]:
        browser = await self._ensure_stealth_browser()
        await self._rate_limiter.wait(url)
        async with self._semaphore:
            context = await browser.new_context(
                locale="en-GB", viewport={"width": 1440, "height": 900}
            )
            page = await context.new_page()
            try:
                response = await page.goto(url, timeout=int(_TIMEOUT_S * 1000))
                status = response.status if response is not None else 0
                content_type = (
                    response.headers.get("content-type", "") if response is not None else ""
                )
                # Client-rendered pages need a beat after navigation to populate.
                await page.wait_for_timeout(_STEALTH_SETTLE_MS)
                html = await page.content()
            finally:
                await context.close()
        return html, status, content_type

    async def _fetch_rendered(self, url: str) -> tuple[str, int, str]:
        browser = await self._ensure_browser()
        await self._rate_limiter.wait(url)
        async with self._semaphore:
            page = await browser.new_page(user_agent=_USER_AGENT)
            try:
                response = await page.goto(url, timeout=int(_TIMEOUT_S * 1000))
                status = response.status if response is not None else 0
                content_type = (
                    response.headers.get("content-type", "") if response is not None else ""
                )
                html = await page.content()
            finally:
                await page.close()
        return html, status, content_type

    async def _ensure_browser(self) -> Browser:
        """Launch Chromium once, installing it on first use if absent."""
        # Single-flight. Without this lock, fetch_pages(js_render=True) fans out N
        # coroutines that each see `_browser is None` and each launch a browser.
        async with self._browser_lock:
            if self._browser is not None:
                return self._browser

            playwright = await _start_playwright()
            try:
                browser = await self._launch(playwright)
            except Exception:
                log.info("Chromium unavailable -- installing it now (one-time, ~130MB download)")
                await _install_chromium()
                try:
                    browser = await self._launch(playwright)
                except Exception:
                    # Stop the driver so a failed launch does not leak a subprocess
                    # for the lifetime of the server.
                    await playwright.stop()
                    raise

            self._playwright = playwright
            self._browser = browser
            return browser

    async def _launch(self, playwright: Playwright) -> Browser:
        if self._config.user_data_dir:
            # A persistent context inherits the user's logged-in Chrome sessions,
            # which is what replaces a cookie-passthrough browser extension.
            context = await playwright.chromium.launch_persistent_context(
                self._config.user_data_dir, headless=True
            )
            browser = context.browser
            if browser is None:
                raise RuntimeError(
                    "Persistent Chrome context exposed no browser handle; check "
                    "SCRAPER_CHROME_USER_DATA_DIR points at a valid profile"
                )
            return browser
        return await playwright.chromium.launch(headless=True)

    def _build_result(
        self, url: str, html: str, status: int, content_type: str, via: str = "httpx"
    ) -> FetchResult:
        blocked, reason = _detect_block(status, html)
        if blocked:
            self._rate_limiter.record_block(url)
            log.info(
                "Block on %s (%s); backing off to %.1fs",
                url,
                reason,
                self._rate_limiter.current_delay(url),
            )
        else:
            self._rate_limiter.record_success(url)

        is_html = _is_html(content_type, html)
        return FetchResult(
            url=url,
            html=html,
            markdown=html_to_markdown(html) if (is_html and not blocked) else "",
            status_code=status,
            title=page_title(html) if is_html else "",
            links_count=_count_links(html) if is_html else 0,
            content_type=content_type,
            blocked=blocked,
            block_reason=reason,
            via=via,
        )


async def _start_playwright() -> Playwright:
    """Start the Playwright driver. Separated so tests can patch a single seam."""
    from playwright.async_api import async_playwright

    return await async_playwright().start()


def _brief(exc: BaseException) -> str:
    message = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _origin_of(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/"


def _validate_url(url: str) -> None:
    scheme = urlparse(url).scheme
    if scheme not in ("http", "https"):
        raise ValueError(f"URL must use the http or https scheme, got: {scheme!r} in {url!r}")


def _detect_block(status: int, html: str) -> tuple[bool, str | None]:
    """A block is a refusal to serve content, not merely an unhappy status code."""
    if any(signature in html for signature in _CHALLENGE_SIGNATURES):
        return True, "challenge"
    if status in _BLOCK_STATUSES:
        # Trustpilot answers 403 while still delivering the whole reviews page.
        # Trusting the status alone would blank a megabyte of real content.
        if _looks_like_content(html):
            return False, None
        return True, f"http_{status}"
    return False, None


def _looks_like_content(html: str) -> bool:
    """True when a body carries enough substance to be the real page."""
    if len(html) < _CONTENT_MIN_HTML:
        return False
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)
    links = soup.find_all("a", href=True)
    return len(text) >= _CONTENT_MIN_TEXT and len(links) >= _CONTENT_MIN_LINKS


def _is_texty(content_type: str) -> bool:
    ct = content_type.lower()
    return (not ct) or ct.startswith("text/") or "html" in ct or "xml" in ct or "json" in ct


def _is_html(content_type: str, html: str) -> bool:
    if not html:
        return False
    ct = content_type.lower()
    return (not ct) or "html" in ct or ct.startswith("text/")


def _count_links(html: str) -> int:
    return len(BeautifulSoup(html, "lxml").find_all("a", href=True))


async def _install_chromium() -> None:
    """Run `playwright install chromium` without blocking the event loop."""
    import sys

    # subprocess.run would freeze every in-flight request for the whole download.
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "playwright",
        "install",
        "chromium",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(
            "Failed to install Chromium automatically. "
            "Run `playwright install chromium` yourself.\n"
            + stdout.decode(errors="replace")[-2000:]
        )
