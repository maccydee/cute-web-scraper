import json
from unittest.mock import AsyncMock

import pytest

from cute_web_scraper.config import Config
from cute_web_scraper.scraper import FetchResult
from cute_web_scraper.server import ScraperHolder, create_server
from cute_web_scraper.store import ResultStore

PRODUCT_HTML = """
<html><head><script type="application/ld+json">
{"@type":"Product","name":"Widget","offers":{"price":"19.99","priceCurrency":"GBP"}}
</script></head><body><a href="/x">x</a></body></html>
"""


def _page(url: str = "https://example.com", **overrides) -> FetchResult:
    base = dict(
        url=url,
        html=PRODUCT_HTML,
        markdown="# Widget\n\nsome body text",
        status_code=200,
        title="Widget",
        links_count=1,
        content_type="text/html",
        blocked=False,
        block_reason=None,
    )
    base.update(overrides)
    return FetchResult(**base)


@pytest.fixture
def store(tmp_path):
    return ResultStore(tmp_path / "r.db")


@pytest.fixture
def holder(store, tmp_path):
    config = Config(
        delay_ms=0,
        max_concurrent=5,
        auth_token=None,
        user_data_dir=None,
        cache_ttl_s=0,
        cache_max_entries=10,
        db_path=tmp_path / "r.db",
        max_inline_chars=2000,
    )
    h = ScraperHolder()
    h.set(AsyncMock(fetch=AsyncMock(return_value=_page())), store=store, config=config)
    return h


@pytest.fixture
def mcp(holder):
    return create_server(holder)


async def _json(mcp, name: str, args: dict):
    result = await mcp.call_tool(name, args)
    return json.loads(result.content[0].text)


async def _text(mcp, name: str, args: dict) -> str:
    result = await mcp.call_tool(name, args)
    return result.content[0].text


# ------------------------------------------------------------------ registration


async def test_all_expected_tools_registered(mcp):
    names = {t.name for t in await mcp.list_tools()}
    assert {
        "fetch_page",
        "fetch_pages",
        "crawl_site",
        "analyze_website",
        "extract_emails",
        "extract_phones",
        "extract_links",
        "extract_social_links",
        "extract_products",
        "list_shopify_collections",
        "extract_shopify_store",
        "list_tables",
        "get_table",
        "query_table",
        "export_table",
        "drop_table",
    } <= names


async def test_prompts_registered(mcp):
    names = {p.name for p in await mcp.list_prompts()}
    assert {"scrape_site", "scrape_shopify_store", "find_contacts", "compare_prices"} <= names


async def test_prompt_renders_with_arguments(mcp):
    got = await mcp.get_prompt("scrape_site", {"url": "https://x.com", "table_name": "t"})
    text = got.messages[0].content.text
    assert "https://x.com" in text
    assert "save_as='t'" in text


# ------------------------------------------------------------------------ save_as


async def test_fetch_pages_save_as_returns_summary_not_content(mcp, holder):
    payload = await _json(
        mcp, "fetch_pages", {"urls": ["https://a.com", "https://b.com"], "save_as": "pages"}
    )
    assert payload["saved_to"] == "pages"
    assert payload["row_count"] == 2
    assert "markdown" in payload["columns"]
    # The full markdown must not come back inline; only a trimmed sample.
    assert "results" not in payload
    assert holder.require_store().get_table("pages").row_count == 2


async def test_extract_tool_save_as(mcp, holder):
    payload = await _json(mcp, "extract_links", {"urls": ["https://a.com"], "save_as": "links"})
    assert payload["saved_to"] == "links"
    assert payload["row_count"] >= 1


async def test_extract_products_save_as(mcp, holder):
    payload = await _json(mcp, "extract_products", {"urls": ["https://a.com"], "save_as": "p"})
    assert payload["row_count"] == 1
    rows = holder.require_store().get_table("p", sample=1).sample
    assert rows[0]["name"] == "Widget"
    assert rows[0]["price"] == 19.99


async def test_save_as_with_invalid_table_name_reports_error(mcp):
    payload = await _json(mcp, "fetch_pages", {"urls": ["https://a.com"], "save_as": "bad name"})
    assert "error" in payload


async def test_save_as_with_no_usable_rows(mcp, holder):
    holder.set(AsyncMock(fetch=AsyncMock(side_effect=RuntimeError("down"))))
    payload = await _json(mcp, "fetch_pages", {"urls": ["https://a.com"], "save_as": "t"})
    assert payload["row_count"] == 0
    assert payload["errors"][0]["error"] == "RuntimeError: down"


# -------------------------------------------------------------- truncation guard


async def test_fetch_pages_inline_output_is_capped(mcp, holder):
    """One call must not be able to fill the conversation."""
    huge = _page(markdown="x" * 5000)
    holder.set(AsyncMock(fetch=AsyncMock(return_value=huge)))
    text = await _text(mcp, "fetch_pages", {"urls": [f"https://a.com/{i}" for i in range(20)]})
    assert len(text) < 3000
    assert "truncated" in text
    assert "save_as" in text


async def test_fetch_page_inline_output_is_capped(mcp, holder):
    holder.set(AsyncMock(fetch=AsyncMock(return_value=_page(markdown="y" * 10_000))))
    text = await _text(mcp, "fetch_page", {"url": "https://a.com"})
    assert len(text) < 3000
    assert "truncated" in text


async def test_small_output_is_not_truncated(mcp):
    text = await _text(mcp, "fetch_page", {"url": "https://a.com"})
    assert "truncated" not in text
    assert "# Widget" in text


async def test_saved_sample_trims_long_fields(mcp, holder):
    holder.set(AsyncMock(fetch=AsyncMock(return_value=_page(markdown="z" * 9000))))
    payload = await _json(mcp, "fetch_pages", {"urls": ["https://a.com"], "save_as": "big"})
    sample_markdown = payload["sample"][0]["markdown"]
    assert len(sample_markdown) < 500
    assert "chars)" in sample_markdown
    # The full value is still stored intact.
    result = holder.require_store().query("SELECT LENGTH(markdown) AS n FROM big")
    assert result.rows[0]["n"] == 9000


# -------------------------------------------------------------------- table tools


async def test_query_table_aggregates(mcp, holder):
    holder.require_store().save(
        "cat",
        [
            {"vendor": "A", "price": 10.0},
            {"vendor": "A", "price": 20.0},
            {"vendor": "B", "price": 5.0},
        ],
    )
    payload = await _json(
        mcp,
        "query_table",
        {"sql": "SELECT vendor, COUNT(*) AS n, AVG(price) AS avg FROM cat GROUP BY vendor"},
    )
    by_vendor = {r["vendor"]: r for r in payload["rows"]}
    assert by_vendor["A"]["n"] == 2
    assert by_vendor["A"]["avg"] == pytest.approx(15.0)


async def test_query_table_rejects_writes(mcp, holder):
    holder.require_store().save("cat", [{"a": 1}])
    payload = await _json(mcp, "query_table", {"sql": "DELETE FROM cat"})
    assert "error" in payload
    assert holder.require_store().get_table("cat").row_count == 1


async def test_list_and_get_table(mcp, holder):
    holder.require_store().save("t1", [{"a": 1}])
    listed = await _json(mcp, "list_tables", {})
    assert listed["count"] == 1
    got = await _json(mcp, "get_table", {"name": "t1"})
    assert got["row_count"] == 1


async def test_get_missing_table_reports_error(mcp):
    payload = await _json(mcp, "get_table", {"name": "nope"})
    assert "error" in payload


async def test_export_table(mcp, holder, tmp_path):
    holder.require_store().save("t", [{"a": 1, "b": "x"}])
    payload = await _json(
        mcp, "export_table", {"name": "t", "fmt": "csv", "dest_dir": str(tmp_path)}
    )
    assert payload["path"].endswith("t.csv")
    assert payload["bytes"] > 0


async def test_drop_table(mcp, holder):
    holder.require_store().save("t", [{"a": 1}, {"a": 2}])
    payload = await _json(mcp, "drop_table", {"name": "t"})
    assert payload["rows_deleted"] == 2
    assert holder.require_store().list_tables() == []
