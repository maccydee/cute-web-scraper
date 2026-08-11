"""Site discovery: sitemaps, link crawling, and platform detection.

robots.txt is read ONLY to locate `Sitemap:` directives. Its `Disallow` rules are
never consulted -- see ASSUMPTIONS A-008. Politeness is the adaptive per-domain
delay in rate_limiter.py, not robots.txt.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Protocol
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
_SITEMAP_TIMEOUT_S = 15.0
_MAX_SITEMAP_DEPTH = 3

_PLATFORM_SIGNALS: dict[str, tuple[str, ...]] = {
    "shopify": ("Shopify.theme", "cdn.shopify.com", "x-shopify"),
    "wordpress": ("WordPress", "wp-content", "wp-json"),
    "squarespace": ("squarespace.com", "Squarespace"),
    "wix": ("wix.com", "x-wix-"),
    "webflow": ("webflow.com", "Webflow"),
}
_SHELL_MIN_HTML = 500
_SHELL_MAX_TEXT = 200
_SHELL_MAX_LINKS = 3
"""Thresholds for spotting an unrendered shell: real markup, almost no content."""

_JS_SIGNALS = (
    "__NEXT_DATA__",
    "window.__NUXT__",
    "ng-version",
    "data-reactroot",
    "__remixContext",
)


class SupportsFetch(Protocol):
    async def fetch(self, url: str, *, js_render: bool = False) -> Any: ...


async def discover_sitemap_urls(base_url: str, client: httpx.AsyncClient) -> list[str]:
    """Page URLs from the site's sitemap, following sitemap indexes."""
    urls = await _collect_from_sitemaps([urljoin(base_url, "/sitemap.xml")], client, depth=0)
    if urls:
        return urls
    from_robots = await _sitemaps_from_robots(base_url, client)
    if from_robots:
        return await _collect_from_sitemaps(from_robots, client, depth=0)
    return []


async def _collect_from_sitemaps(
    sitemap_urls: list[str], client: httpx.AsyncClient, *, depth: int
) -> list[str]:
    if depth > _MAX_SITEMAP_DEPTH:
        return []
    pages: list[str] = []
    for sitemap_url in sitemap_urls:
        body = await _get_text(sitemap_url, client)
        if body is None:
            continue
        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError:
            log.debug("Unparseable sitemap at %s", sitemap_url)
            continue
        # A <sitemapindex> lists child sitemaps, not pages. Treating its <loc> values
        # as page URLs hands .xml files to fetch_pages.
        if root.tag == f"{_SITEMAP_NS}sitemapindex":
            children = [el.text.strip() for el in root.iter(f"{_SITEMAP_NS}loc") if el.text]
            pages.extend(await _collect_from_sitemaps(children, client, depth=depth + 1))
            continue
        for element in root.iter(f"{_SITEMAP_NS}loc"):
            if element.text:
                pages.append(element.text.strip())
    return pages


async def _sitemaps_from_robots(base_url: str, client: httpx.AsyncClient) -> list[str]:
    body = await _get_text(urljoin(base_url, "/robots.txt"), client)
    if body is None:
        return []
    return [
        line.split(":", 1)[1].strip()
        for line in body.splitlines()
        if line.lower().startswith("sitemap:")
    ]


async def _get_text(url: str, client: httpx.AsyncClient) -> str | None:
    try:
        response = await client.get(url, timeout=_SITEMAP_TIMEOUT_S)
    except httpx.HTTPError as exc:
        log.debug("Sitemap fetch failed for %s: %s", url, exc)
        return None
    if response.status_code != 200:
        return None
    return response.text


async def crawl_by_links(base_url: str, limit: int, scraper: SupportsFetch) -> list[str]:
    """Breadth-first same-domain crawl, going through the Scraper so it stays rate-limited."""
    base_domain = urlparse(base_url).netloc.lower()
    visited: list[str] = []
    seen: set[str] = {base_url}
    queue: list[str] = [base_url]

    while queue and len(visited) < limit:
        url = queue.pop(0)
        try:
            # Via the Scraper, not a bare client -- this is what applies the
            # per-domain delay.
            result = await scraper.fetch(url)
        except Exception as exc:  # noqa: BLE001
            log.debug("Crawl fetch failed for %s: %s", url, exc)
            continue
        visited.append(url)
        if getattr(result, "blocked", False):
            continue
        for href in re.findall(r'href=["\']([^"\']+)["\']', getattr(result, "html", "")):
            absolute = urljoin(url, href).split("#")[0]
            parsed = urlparse(absolute)
            if parsed.scheme not in ("http", "https"):
                continue
            if parsed.netloc.lower() != base_domain or absolute in seen:
                continue
            seen.add(absolute)
            queue.append(absolute)
    return visited


def detect_platform(html: str, headers: dict[str, str]) -> str:
    # Header NAMES carry the signal for several platforms, e.g. X-Wix-Request-Id.
    # Searching only the values means those signals can never fire.
    haystack = " ".join([html, " ".join(headers.keys()), " ".join(headers.values())]).lower()
    for platform, signals in _PLATFORM_SIGNALS.items():
        if any(signal.lower() in haystack for signal in signals):
            return platform
    return "unknown"


def detect_requires_js(html: str) -> bool:
    """True when the page needs a browser to produce its content.

    Framework markers catch the common cases, but not all of them: Reddit's shell
    carries none of them and still renders entirely client-side, so it was reported
    as static while returning a page whose whole body text was the word "Reddit".
    The structural check covers any framework — a document with markup but almost
    no text and no links has not been rendered yet.
    """
    if any(signal in html for signal in _JS_SIGNALS):
        return True
    if len(html) < _SHELL_MIN_HTML:
        return False
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)
    links = soup.find_all("a", href=True)
    return len(text) < _SHELL_MAX_TEXT and len(links) < _SHELL_MAX_LINKS
