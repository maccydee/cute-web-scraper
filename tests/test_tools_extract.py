import json
from unittest.mock import AsyncMock

import httpx
import pytest

from cute_web_scraper.scraper import FetchResult
from cute_web_scraper.server import ScraperHolder, create_server

HTML = (
    "<p>Contact hi@ex.com or call +44 7700 900123</p>"
    '<a href="/about">About</a><a href="https://github.com/maccydee">GH</a>'
)


def _realistic(url: str = "https://example.com", **overrides) -> FetchResult:
    """html and markdown deliberately differ -- markdown has no <a href> tags."""
    base = dict(
        url=url,
        html=HTML,
        markdown="Contact hi@ex.com or call +44 7700 900123\n\n[About](/about)",
        status_code=200,
        title="",
        links_count=2,
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


async def _json(mcp, name: str, args: dict):
    result = await mcp.call_tool(name, args)
    return json.loads(result.content[0].text)


async def test_all_extract_tools_registered(mcp):
    names = {t.name for t in await mcp.list_tools()}
    assert {
        "extract_emails",
        "extract_phones",
        "extract_links",
        "extract_social_links",
    } <= names


async def test_extract_emails(mcp, holder):
    holder.set(AsyncMock(fetch=AsyncMock(return_value=_realistic())))
    payload = await _json(mcp, "extract_emails", {"urls": ["https://example.com"]})
    assert any(r["value"] == "hi@ex.com" for r in payload["results"])


async def test_extract_links_uses_html_not_markdown(mcp, holder):
    """Regression: v1 passed markdown, so this tool always returned []."""
    holder.set(AsyncMock(fetch=AsyncMock(return_value=_realistic())))
    payload = await _json(mcp, "extract_links", {"urls": ["https://example.com"]})
    values = [r["value"] for r in payload["results"]]
    assert "https://example.com/about" in values


async def test_extract_social_links_uses_html(mcp, holder):
    holder.set(AsyncMock(fetch=AsyncMock(return_value=_realistic())))
    payload = await _json(mcp, "extract_social_links", {"urls": ["https://example.com"]})
    assert any(r["platform"] == "github" for r in payload["results"])


async def test_partial_failure_is_reported(mcp, holder):
    async def fetch(url, *, js_render=False):
        if "bad" in url:
            raise httpx.ConnectError("boom")
        return _realistic(url=url)

    holder.set(AsyncMock(fetch=fetch))
    payload = await _json(
        mcp, "extract_emails", {"urls": ["https://example.com", "https://bad.com"]}
    )
    assert len(payload["results"]) >= 1
    assert payload["errors"][0]["url"] == "https://bad.com"


async def test_blocked_page_is_reported_not_silently_empty(mcp, holder):
    holder.set(
        AsyncMock(
            fetch=AsyncMock(return_value=_realistic(blocked=True, block_reason="http_429", html=""))
        )
    )
    payload = await _json(mcp, "extract_emails", {"urls": ["https://example.com"]})
    assert payload["results"] == []
    assert "http_429" in payload["errors"][0]["error"]
