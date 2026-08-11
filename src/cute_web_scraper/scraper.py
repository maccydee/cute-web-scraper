"""Two-tier scraping engine: httpx for static pages, Playwright for JS-rendered pages."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
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

_CHALLENGE_SIGNATURES = (
    "cf-browser-verification",
    "/cdn-cgi/challenge-platform",
    "__cf_chl",
    "<title>Just a moment",
    "Checking your browser before accessing",
    "Attention Required! | Cloudflare",
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
            html, status, content_type = await self._fetch_rendered(url)
        else:
            html, status, content_type = await self._fetch_static(url)

        result = self._build_result(url, html, status, content_type)
        self._cache.put(url, js_render, result)
        return result

    async def _fetch_static(self, url: str) -> tuple[str, int, str]:
        await self._rate_limiter.wait(url)
        async with self._semaphore:
            log.debug("GET %s", url)
            response = await self.http.get(url)
        content_type = response.headers.get("content-type", "")
        # Decoding a PDF or image as text produces garbage; skip the body entirely.
        body = response.text if _is_texty(content_type) else ""
        return body, response.status_code, content_type

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

    def _build_result(self, url: str, html: str, status: int, content_type: str) -> FetchResult:
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
        )


async def _start_playwright() -> Playwright:
    """Start the Playwright driver. Separated so tests can patch a single seam."""
    from playwright.async_api import async_playwright

    return await async_playwright().start()


def _validate_url(url: str) -> None:
    scheme = urlparse(url).scheme
    if scheme not in ("http", "https"):
        raise ValueError(f"URL must use the http or https scheme, got: {scheme!r} in {url!r}")


def _detect_block(status: int, html: str) -> tuple[bool, str | None]:
    if status in _BLOCK_STATUSES:
        return True, f"http_{status}"
    if any(signature in html for signature in _CHALLENGE_SIGNATURES):
        return True, "challenge"
    return False, None


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
