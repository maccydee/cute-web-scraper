"""MCP tool surface."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin

from mcp.server.mcpserver import MCPServer

from . import extractors
from .scraper import FetchResult, Scraper

log = logging.getLogger(__name__)

ExtractorFn = Callable[[str, str], list[dict[str, str]]]


class ScraperHolder:
    """Indirection so tools can be registered before the Scraper exists.

    MCP 2.0's Context is unavailable outside a live request, so a ctx-based lookup
    cannot be driven by call_tool in tests. A holder keeps production wiring honest
    via the entry point while leaving the real dispatch path testable.
    """

    def __init__(self, scraper: Scraper | None = None) -> None:
        self._scraper: Any = scraper

    def set(self, scraper: Any) -> None:
        self._scraper = scraper

    def require(self) -> Any:
        if self._scraper is None:
            raise RuntimeError("Scraper is not initialised; the server lifespan did not run")
        return self._scraper


def create_server(holder: ScraperHolder) -> MCPServer:
    mcp = MCPServer("cute-web-scraper")
    _register_fetch_tools(mcp, holder)
    _register_discovery_tools(mcp, holder)
    _register_extract_tools(mcp, holder)
    return mcp


def _register_fetch_tools(mcp: MCPServer, holder: ScraperHolder) -> None:
    @mcp.tool(
        description=(
            "Fetch one web page and return its content as clean markdown with metadata. "
            "Set js_render=true for pages that need JavaScript to render (SPAs, "
            "infinite-scroll listings, most modern storefronts)."
        )
    )
    async def fetch_page(url: str, js_render: bool = False) -> str:
        log.debug("tool=fetch_page url=%s js_render=%s", url, js_render)
        try:
            result = await holder.require().fetch(url, js_render=js_render)
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller as text
            log.error("tool=fetch_page failed: %s", exc)
            return f"Error fetching {url}: {exc}"
        return _render_page(result)

    @mcp.tool(
        description=(
            "Fetch many web pages in parallel. Returns JSON with a `results` array "
            "(url, title, markdown, status_code, blocked) and an `errors` array for "
            "URLs that failed. Set js_render=true for JavaScript-heavy pages."
        )
    )
    async def fetch_pages(urls: list[str], js_render: bool = False) -> str:
        scraper = holder.require()
        settled = await asyncio.gather(
            *(scraper.fetch(u, js_render=js_render) for u in urls),
            return_exceptions=True,
        )
        results: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for url, outcome in zip(urls, settled, strict=True):
            if isinstance(outcome, BaseException):
                errors.append({"url": url, "error": str(outcome)})
                continue
            results.append(
                {
                    "url": outcome.url,
                    "title": outcome.title,
                    "status_code": outcome.status_code,
                    # Dropping this makes a blocked page indistinguishable from a
                    # genuinely empty one.
                    "blocked": outcome.blocked,
                    "block_reason": outcome.block_reason,
                    "markdown": outcome.markdown,
                }
            )
        return json.dumps({"results": results, "errors": errors}, ensure_ascii=False)


def _register_discovery_tools(mcp: MCPServer, holder: ScraperHolder) -> None:
    @mcp.tool(
        description=(
            "Discover the pages on a website. Prefers the site's sitemap (following "
            "sitemap indexes and robots.txt), and falls back to following links. "
            "Returns JSON with `urls`, `count`, `source` and `truncated`. Run this "
            "before fetch_pages to scrape a whole site."
        )
    )
    async def crawl_site(url: str, limit: int = 500) -> str:
        from .crawler import crawl_by_links, discover_sitemap_urls

        scraper = holder.require()
        try:
            urls = await discover_sitemap_urls(url, scraper.http)
            source = "sitemap"
            if not urls:
                urls = await crawl_by_links(url, limit=limit, scraper=scraper)
                source = "links"
        except Exception as exc:  # noqa: BLE001
            log.error("tool=crawl_site failed: %s", exc)
            return json.dumps({"url": url, "error": str(exc), "urls": []})

        truncated = len(urls) > limit
        return json.dumps(
            {
                "url": url,
                "source": source,
                "count": len(urls[:limit]),
                "truncated": truncated,
                "urls": urls[:limit],
            },
            ensure_ascii=False,
        )

    @mcp.tool(
        description=(
            "Inspect a website before scraping it: detects the platform (Shopify, "
            "WordPress, Wix, ...), locates its sitemap, estimates how many pages it "
            "has, and reports whether JavaScript rendering is needed."
        )
    )
    async def analyze_website(url: str) -> str:
        from .crawler import detect_platform, detect_requires_js, discover_sitemap_urls

        scraper = holder.require()
        try:
            page = await scraper.fetch(url)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"url": url, "error": str(exc)})

        try:
            sitemap_urls = await discover_sitemap_urls(url, scraper.http)
        except Exception:  # noqa: BLE001
            sitemap_urls = []

        return json.dumps(
            {
                "url": url,
                "status_code": page.status_code,
                "blocked": page.blocked,
                "platform": detect_platform(page.html, {}),
                "sitemap_url": (urljoin(url, "/sitemap.xml") if sitemap_urls else None),
                "page_count_estimate": len(sitemap_urls),
                "requires_js": detect_requires_js(page.html),
            },
            ensure_ascii=False,
        )


def _register_extract_tools(mcp: MCPServer, holder: ScraperHolder) -> None:
    specs: list[tuple[str, ExtractorFn, str]] = [
        (
            "extract_emails",
            extractors.extract_emails,
            "Scan a list of URLs for email addresses. Returns JSON with `results` "
            "({url, value, context}) and `errors`.",
        ),
        (
            "extract_phones",
            extractors.extract_phones,
            "Scan a list of URLs for phone numbers. Returns JSON with `results` "
            "({url, value, context}) and `errors`.",
        ),
        (
            "extract_links",
            extractors.extract_links,
            "Collect every hyperlink from a list of URLs, resolved to absolute URLs. "
            "Returns JSON with `results` ({url, value, context}) and `errors`.",
        ),
        (
            "extract_social_links",
            extractors.extract_social_links,
            "Find social media profile links (LinkedIn, X, Facebook, Instagram, "
            "YouTube, TikTok, GitHub, Pinterest) across a list of URLs. Returns JSON "
            "with `results` ({url, platform, value}) and `errors`.",
        ),
    ]
    for name, fn, description in specs:
        mcp.add_tool(_build_extract_tool(holder, fn), name=name, description=description)


def _build_extract_tool(holder: ScraperHolder, extractor: ExtractorFn) -> Callable[..., Any]:
    # A plain closure registered via add_tool. functools.wraps would copy the
    # extractor's __annotations__/__wrapped__ onto this function and publish a
    # wrong tool schema.
    async def _extract(urls: list[str], js_render: bool = False) -> str:
        scraper = holder.require()
        settled = await asyncio.gather(
            *(scraper.fetch(u, js_render=js_render) for u in urls),
            return_exceptions=True,
        )
        results: list[dict[str, str]] = []
        errors: list[dict[str, str]] = []
        for url, outcome in zip(urls, settled, strict=True):
            if isinstance(outcome, BaseException):
                errors.append({"url": url, "error": str(outcome)})
                continue
            if outcome.blocked:
                errors.append({"url": url, "error": f"blocked: {outcome.block_reason}"})
                continue
            # Raw HTML, never markdown. Link and social extraction parse <a href>
            # tags, which markdown does not contain.
            results.extend(extractor(outcome.html, outcome.url))
        return json.dumps({"results": results, "errors": errors}, ensure_ascii=False)

    return _extract


def _render_page(result: FetchResult) -> str:
    front_matter = "\n".join(
        [
            "---",
            f"url: {result.url}",
            f"status_code: {result.status_code}",
            f"title: {result.title}",
            f"links_count: {result.links_count}",
            f"blocked: {str(result.blocked).lower()}",
            f"block_reason: {result.block_reason or ''}",
            "---",
        ]
    )
    if result.blocked:
        return (
            f"{front_matter}\n\n"
            f"This page was blocked ({result.block_reason}). The rate limiter has "
            "increased the delay for this domain; retrying later may succeed. "
            "For challenge pages, try js_render=true."
        )
    return f"{front_matter}\n\n{result.markdown}"
