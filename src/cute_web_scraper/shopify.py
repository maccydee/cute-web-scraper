"""Shopify storefront extraction via the public products.json endpoint.

Every Shopify store exposes /products.json and /collections.json without auth, so a
whole catalogue can be read as structured data rather than scraped from HTML.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urljoin

import httpx

log = logging.getLogger(__name__)

_PAGE_SIZE = 250
_MAX_PAGES = 100
_TIMEOUT_S = 30.0


class ShopifyError(Exception):
    """Raised when a store does not expose a usable Shopify JSON endpoint."""


async def fetch_store_products(
    store_url: str,
    client: httpx.AsyncClient,
    *,
    max_products: int | None = None,
) -> list[dict[str, Any]]:
    """Every product in the store, paginating until the endpoint runs dry."""
    return await _paginate(store_url, client, "products.json", "products", max_products)


async def fetch_collections(
    store_url: str,
    client: httpx.AsyncClient,
    *,
    max_collections: int | None = None,
) -> list[dict[str, Any]]:
    raw = await _paginate(store_url, client, "collections.json", "collections", max_collections)
    return [
        {
            "title": c.get("title"),
            "handle": c.get("handle"),
            "products_count": c.get("products_count"),
            "url": urljoin(_base(store_url), f"/collections/{c.get('handle')}"),
        }
        for c in raw
    ]


async def _paginate(
    store_url: str,
    client: httpx.AsyncClient,
    endpoint: str,
    key: str,
    limit: int | None,
) -> list[dict[str, Any]]:
    base = _base(store_url)
    collected: list[dict[str, Any]] = []
    for page in range(1, _MAX_PAGES + 1):
        url = urljoin(base, f"/{endpoint}")
        try:
            response = await client.get(
                url, params={"limit": _PAGE_SIZE, "page": page}, timeout=_TIMEOUT_S
            )
        except httpx.HTTPError as exc:
            raise ShopifyError(f"Could not reach {url}: {exc}") from None

        if response.status_code == 404:
            raise ShopifyError(
                f"{base} does not expose {endpoint} — it is probably not a Shopify store. "
                "Use crawl_site and fetch_pages instead."
            )
        if response.status_code != 200:
            raise ShopifyError(f"{url} returned HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError:
            # A storefront that answers 200 with HTML is not a Shopify JSON endpoint.
            raise ShopifyError(
                f"{url} did not return JSON — {base} is probably not a Shopify store."
            ) from None

        batch = payload.get(key) if isinstance(payload, dict) else None
        if not batch:
            break
        collected.extend(batch)
        if limit is not None and len(collected) >= limit:
            return collected[:limit]
        if len(batch) < _PAGE_SIZE:
            break
    return collected


def variant_rows(products: list[dict[str, Any]], store_url: str) -> list[dict[str, Any]]:
    """Flatten products to one row per variant, the shape a catalogue export wants."""
    base = _base(store_url)
    rows: list[dict[str, Any]] = []
    for product in products:
        handle = product.get("handle")
        images = product.get("images") or []
        default_image = images[0].get("src") if images and isinstance(images[0], dict) else None
        tags = product.get("tags")
        for variant in product.get("variants") or []:
            rows.append(
                {
                    "product_title": product.get("title"),
                    "variant_title": variant.get("title"),
                    "sku": variant.get("sku") or None,
                    "price": _to_float(variant.get("price")),
                    "compare_at_price": _to_float(variant.get("compare_at_price")),
                    "available": bool(variant.get("available")),
                    "options": _options(variant),
                    "vendor": product.get("vendor"),
                    "product_type": product.get("product_type"),
                    "tags": ", ".join(tags) if isinstance(tags, list) else tags,
                    "image": default_image,
                    "product_url": urljoin(base, f"/products/{handle}") if handle else None,
                    "product_id": product.get("id"),
                    "variant_id": variant.get("id"),
                }
            )
    return rows


def _options(variant: dict[str, Any]) -> str | None:
    parts = [variant.get(f"option{i}") for i in (1, 2, 3)]
    present = [str(p) for p in parts if p]
    return " / ".join(present) if present else None


def _base(store_url: str) -> str:
    url = store_url.strip()
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    return url.rstrip("/") + "/"


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None
