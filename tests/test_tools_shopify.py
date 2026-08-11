import json
from unittest.mock import AsyncMock

import httpx
import pytest

from cute_web_scraper.config import Config
from cute_web_scraper.server import ScraperHolder, create_server
from cute_web_scraper.store import ResultStore

PRODUCTS = {
    "products": [
        {
            "id": 1,
            "title": "Trail Runner",
            "handle": "trail-runner",
            "vendor": "Fellrunner",
            "images": [{"src": "https://cdn/x.jpg"}],
            "variants": [
                {"id": 11, "title": "UK 8", "sku": "A", "price": "129.99", "available": True},
                {"id": 12, "title": "UK 9", "sku": "B", "price": "139.99", "available": False},
            ],
        }
    ]
}


@pytest.fixture
def holder(tmp_path):
    config = Config(
        delay_ms=0,
        max_concurrent=5,
        auth_token=None,
        user_data_dir=None,
        cache_ttl_s=0,
        cache_max_entries=10,
        db_path=tmp_path / "r.db",
        max_inline_chars=50_000,
    )
    scraper = AsyncMock()
    scraper.http = httpx.AsyncClient()
    h = ScraperHolder()
    h.set(scraper, store=ResultStore(tmp_path / "r.db"), config=config)
    return h


@pytest.fixture
def mcp(holder):
    return create_server(holder)


async def _json(mcp, name: str, args: dict):
    result = await mcp.call_tool(name, args)
    return json.loads(result.content[0].text)


async def test_extract_shopify_store_inline(mcp, holder, httpx_mock):
    httpx_mock.add_response(
        url="https://shop.example/products.json?limit=250&page=1", json=PRODUCTS
    )
    payload = await _json(mcp, "extract_shopify_store", {"store_url": "https://shop.example"})
    await holder.require().http.aclose()
    assert payload["products"] == 1
    assert payload["variants"] == 2
    assert payload["results"][0]["product_title"] == "Trail Runner"
    assert payload["results"][0]["price"] == 129.99


async def test_extract_shopify_store_save_as(mcp, holder, httpx_mock):
    httpx_mock.add_response(
        url="https://shop.example/products.json?limit=250&page=1", json=PRODUCTS
    )
    payload = await _json(
        mcp, "extract_shopify_store", {"store_url": "https://shop.example", "save_as": "cat"}
    )
    await holder.require().http.aclose()
    assert payload["saved_to"] == "cat"
    assert payload["row_count"] == 2
    assert payload["products"] == 1
    assert payload["variants"] == 2
    # Prices stored as REAL so numeric filtering works.
    result = holder.require_store().query("SELECT COUNT(*) AS n FROM cat WHERE price > 130")
    assert result.rows[0]["n"] == 1


async def test_non_shopify_store_reports_error(mcp, holder, httpx_mock):
    httpx_mock.add_response(
        url="https://blog.example/products.json?limit=250&page=1", status_code=404
    )
    payload = await _json(mcp, "extract_shopify_store", {"store_url": "https://blog.example"})
    await holder.require().http.aclose()
    assert "error" in payload
    assert "Shopify" in payload["error"]


async def test_list_shopify_collections(mcp, holder, httpx_mock):
    httpx_mock.add_response(
        url="https://shop.example/collections.json?limit=250&page=1",
        json={"collections": [{"title": "Winter", "handle": "winter", "products_count": 3}]},
    )
    payload = await _json(mcp, "list_shopify_collections", {"store_url": "https://shop.example"})
    await holder.require().http.aclose()
    assert payload["count"] == 1
    assert payload["collections"][0]["handle"] == "winter"
    assert payload["collections"][0]["url"] == "https://shop.example/collections/winter"
