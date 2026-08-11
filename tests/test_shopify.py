import httpx
import pytest

from cute_web_scraper.shopify import (
    ShopifyError,
    fetch_collections,
    fetch_store_products,
    variant_rows,
)

PRODUCTS_PAGE_1 = {
    "products": [
        {
            "id": 1,
            "title": "Trail Runner",
            "handle": "trail-runner",
            "vendor": "Fellrunner",
            "product_type": "Shoes",
            "tags": ["trail", "running"],
            "images": [{"src": "https://cdn/x.jpg"}],
            "variants": [
                {
                    "id": 11,
                    "title": "UK 8 / Red",
                    "sku": "TR-8-R",
                    "price": "129.99",
                    "available": True,
                    "option1": "UK 8",
                    "option2": "Red",
                },
                {
                    "id": 12,
                    "title": "UK 9 / Red",
                    "sku": "TR-9-R",
                    "price": "129.99",
                    "available": False,
                    "option1": "UK 9",
                    "option2": "Red",
                },
            ],
        }
    ]
}
EMPTY = {"products": []}


async def test_short_page_stops_without_a_second_request(httpx_mock):
    """A page below the limit means the catalogue is exhausted; don't waste a request."""
    httpx_mock.add_response(
        url="https://shop.example/products.json?limit=250&page=1", json=PRODUCTS_PAGE_1
    )
    async with httpx.AsyncClient() as client:
        products = await fetch_store_products("https://shop.example", client)
    assert len(products) == 1
    assert products[0]["title"] == "Trail Runner"
    assert len(httpx_mock.get_requests()) == 1


async def test_full_page_triggers_next_request(httpx_mock):
    """A full page means there may be more, so the next page is fetched."""
    full = {"products": [dict(PRODUCTS_PAGE_1["products"][0], id=i) for i in range(250)]}
    httpx_mock.add_response(url="https://shop.example/products.json?limit=250&page=1", json=full)
    httpx_mock.add_response(url="https://shop.example/products.json?limit=250&page=2", json=EMPTY)
    async with httpx.AsyncClient() as client:
        products = await fetch_store_products("https://shop.example", client)
    assert len(products) == 250
    assert len(httpx_mock.get_requests()) == 2


async def test_fetch_store_respects_max_products(httpx_mock):
    httpx_mock.add_response(
        url="https://shop.example/products.json?limit=250&page=1", json=PRODUCTS_PAGE_1
    )
    async with httpx.AsyncClient() as client:
        products = await fetch_store_products("https://shop.example", client, max_products=1)
    assert len(products) == 1


async def test_non_shopify_store_raises(httpx_mock):
    httpx_mock.add_response(
        url="https://notshopify.example/products.json?limit=250&page=1",
        status_code=404,
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(ShopifyError, match="Shopify"):
            await fetch_store_products("https://notshopify.example", client)


async def test_html_response_raises(httpx_mock):
    """A storefront that returns an HTML 200 for products.json is not Shopify."""
    httpx_mock.add_response(
        url="https://fake.example/products.json?limit=250&page=1",
        text="<html><body>Not JSON</body></html>",
        headers={"Content-Type": "text/html"},
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(ShopifyError):
            await fetch_store_products("https://fake.example", client)


async def test_fetch_collections(httpx_mock):
    httpx_mock.add_response(
        url="https://shop.example/collections.json?limit=250&page=1",
        json={
            "collections": [{"id": 5, "title": "Winter", "handle": "winter", "products_count": 12}]
        },
    )
    async with httpx.AsyncClient() as client:
        collections = await fetch_collections("https://shop.example", client)
    assert collections[0]["handle"] == "winter"
    assert collections[0]["products_count"] == 12


def test_variant_rows_one_row_per_variant():
    rows = variant_rows(PRODUCTS_PAGE_1["products"], "https://shop.example")
    assert len(rows) == 2
    first = rows[0]
    assert first["product_title"] == "Trail Runner"
    assert first["variant_title"] == "UK 8 / Red"
    assert first["sku"] == "TR-8-R"
    assert first["price"] == 129.99
    assert first["available"] is True
    assert first["options"] == "UK 8 / Red"
    assert first["image"] == "https://cdn/x.jpg"
    assert first["product_url"] == "https://shop.example/products/trail-runner"
    assert first["vendor"] == "Fellrunner"


def test_variant_rows_marks_unavailable():
    rows = variant_rows(PRODUCTS_PAGE_1["products"], "https://shop.example")
    assert rows[1]["available"] is False


def test_variant_rows_handles_missing_fields():
    minimal = [{"id": 9, "title": "Bare", "handle": "bare", "variants": [{"id": 1}]}]
    rows = variant_rows(minimal, "https://shop.example")
    assert rows[0]["product_title"] == "Bare"
    assert rows[0]["price"] is None
    assert rows[0]["image"] is None


def test_variant_rows_product_with_no_variants_is_skipped():
    assert variant_rows([{"id": 1, "title": "X", "handle": "x", "variants": []}], "https://s") == []
