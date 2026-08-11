import json
from unittest.mock import AsyncMock

import httpx
import pytest

from cute_web_scraper.scraper import FetchResult
from cute_web_scraper.server import ScraperHolder, create_server


def _result(url: str = "https://example.com", **overrides) -> FetchResult:
    base = dict(
        url=url,
        html="<h1>Hello</h1>",
        markdown="# Hello\n\nWorld",
        status_code=200,
        title="Hello",
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


async def _text(mcp, name: str, args: dict) -> str:
    result = await mcp.call_tool(name, args)
    return result.content[0].text


async def test_tools_are_registered(mcp):
    names = {t.name for t in await mcp.list_tools()}
    assert {"fetch_page", "fetch_pages"} <= names


async def test_fetch_page_front_matter(mcp, holder):
    holder.set(AsyncMock(fetch=AsyncMock(return_value=_result())))
    text = await _text(mcp, "fetch_page", {"url": "https://example.com"})
    assert "url: https://example.com" in text
    assert "status_code: 200" in text
    assert "title: Hello" in text
    assert "# Hello" in text


async def test_fetch_page_reports_block(mcp, holder):
    holder.set(
        AsyncMock(
            fetch=AsyncMock(
                return_value=_result(blocked=True, block_reason="http_429", markdown="")
            )
        )
    )
    text = await _text(mcp, "fetch_page", {"url": "https://example.com"})
    assert "blocked: true" in text.lower()
    assert "http_429" in text


async def test_fetch_page_rejects_bad_scheme(mcp, holder):
    holder.set(
        AsyncMock(fetch=AsyncMock(side_effect=ValueError("URL must use the http or https scheme")))
    )
    text = await _text(mcp, "fetch_page", {"url": "ftp://example.com"})
    assert "http" in text


async def test_fetch_page_handles_network_error(mcp, holder):
    """Regression: v1 caught only ValueError, so a dead host raised out of the tool."""
    holder.set(AsyncMock(fetch=AsyncMock(side_effect=httpx.ConnectError("no route"))))
    text = await _text(mcp, "fetch_page", {"url": "https://dead.example"})
    assert "no route" in text


async def test_fetch_pages_returns_results_and_errors(mcp, holder):
    async def fetch(url, *, js_render=False):
        if "bad" in url:
            raise httpx.ConnectError("boom")
        return _result(url=url)

    holder.set(AsyncMock(fetch=fetch))
    text = await _text(mcp, "fetch_pages", {"urls": ["https://a.com", "https://bad.com"]})
    payload = json.loads(text)
    assert len(payload["results"]) == 1
    assert payload["results"][0]["blocked"] is False
    assert len(payload["errors"]) == 1
    assert payload["errors"][0]["url"] == "https://bad.com"


async def test_error_without_a_message_still_names_the_failure(mcp, holder):
    """Found live on asos.com: httpx raises ReadTimeout('') and str(exc) is empty,
    so the tool reported 'Error fetching https://...: ' and said nothing useful."""
    holder.set(AsyncMock(fetch=AsyncMock(side_effect=httpx.ReadTimeout(""))))
    text = await _text(mcp, "fetch_page", {"url": "https://slow.example"})
    assert "ReadTimeout" in text
    assert not text.rstrip().endswith(":")


async def test_batch_error_without_a_message_names_the_failure(mcp, holder):
    holder.set(AsyncMock(fetch=AsyncMock(side_effect=httpx.ConnectTimeout(""))))
    payload = json.loads(await _text(mcp, "fetch_pages", {"urls": ["https://slow.example"]}))
    assert payload["errors"][0]["error"] == "ConnectTimeout"
