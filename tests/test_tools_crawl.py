import json
from unittest.mock import AsyncMock

import httpx
import pytest

from cute_web_scraper.scraper import FetchResult
from cute_web_scraper.server import ScraperHolder, create_server

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/a</loc></url>
  <url><loc>https://example.com/b</loc></url>
</urlset>"""


def _result(**overrides) -> FetchResult:
    base = dict(
        url="https://example.com",
        html='<meta name="generator" content="WordPress 6.5">',
        markdown="",
        status_code=200,
        title="",
        links_count=0,
        content_type="text/html",
        blocked=False,
        block_reason=None,
    )
    base.update(overrides)
    return FetchResult(**base)


@pytest.fixture
def holder():
    return ScraperHolder()


@pytest.fixture
def mcp(holder):
    return create_server(holder)


async def _scraper_with_client():
    scraper = AsyncMock(fetch=AsyncMock(return_value=_result()))
    scraper.http = httpx.AsyncClient()
    return scraper


async def _json(mcp, name: str, args: dict):
    result = await mcp.call_tool(name, args)
    return json.loads(result.content[0].text)


async def test_crawl_site_uses_sitemap(mcp, holder, httpx_mock):
    httpx_mock.add_response(url="https://example.com/sitemap.xml", text=SITEMAP)
    scraper = await _scraper_with_client()
    holder.set(scraper)
    payload = await _json(mcp, "crawl_site", {"url": "https://example.com", "limit": 50})
    await scraper.http.aclose()
    assert payload["source"] == "sitemap"
    assert "https://example.com/a" in payload["urls"]
    assert payload["count"] == 2


async def test_crawl_site_truncates(mcp, holder, httpx_mock):
    httpx_mock.add_response(url="https://example.com/sitemap.xml", text=SITEMAP)
    scraper = await _scraper_with_client()
    holder.set(scraper)
    payload = await _json(mcp, "crawl_site", {"url": "https://example.com", "limit": 1})
    await scraper.http.aclose()
    assert len(payload["urls"]) == 1
    assert payload["truncated"] is True


async def test_analyze_website_shape(mcp, holder, httpx_mock):
    httpx_mock.add_response(url="https://example.com/sitemap.xml", text=SITEMAP)
    scraper = await _scraper_with_client()
    holder.set(scraper)
    payload = await _json(mcp, "analyze_website", {"url": "https://example.com"})
    await scraper.http.aclose()
    assert payload["platform"] == "wordpress"
    assert payload["requires_js"] is False
    assert payload["page_count_estimate"] == 2
    assert set(payload) >= {"url", "platform", "sitemap_url", "requires_js"}


async def test_analyze_website_reports_error(mcp, holder):
    scraper = AsyncMock(fetch=AsyncMock(side_effect=httpx.ConnectError("no route")))
    holder.set(scraper)
    payload = await _json(mcp, "analyze_website", {"url": "https://dead.example"})
    assert "error" in payload
