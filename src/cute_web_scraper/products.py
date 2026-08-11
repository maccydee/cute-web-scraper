"""Structured product extraction from JSON-LD, OpenGraph and microdata.

Most storefronts already publish machine-readable product data. Reading it beats
guessing at CSS selectors, and it degrades gracefully: JSON-LD first, then
OpenGraph, then microdata.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

_FIELDS = (
    "url",
    "name",
    "price",
    "currency",
    "availability",
    "brand",
    "sku",
    "image",
    "rating",
    "review_count",
    "description",
    "source",
)
_PRICE_CLEAN_RE = re.compile(r"[^\d.,\-]")
_IMAGE_EXT_RE = re.compile(r"\.(jpe?g|png|gif|webp|avif|svg|bmp|tiff?)(?:[?#]|$)", re.I)
"""An image value is only trusted if it looks like a URL or an image path.

Pages put all sorts in itemprop="image" — MIME types, icon glyphs, alt text. Found
live: a value of "⚑image/svg+xml" was absolutised into
https://site/product/123/⚑image/svg+xml, which is worse than returning nothing.
A blacklist missed it; requiring the value to actually look like an image does not.
"""


def extract_product(html: str, url: str) -> dict[str, Any]:
    """Best-effort structured product record. `source` names where the data came from."""
    empty = dict.fromkeys(_FIELDS)
    empty["url"] = url
    empty["source"] = "none"
    if not html:
        return empty

    soup = BeautifulSoup(html, "lxml")
    for parser in (_from_json_ld, _from_opengraph, _from_microdata):
        found = parser(soup, url)
        if found is not None:
            record = {**empty, **found, "url": url}
            record["image"] = _clean_image(record.get("image"), url)
            return record
    return empty


def _clean_image(value: Any, url: str) -> str | None:
    text = _text(value)
    if text is None or " " in text:
        return None
    looks_like_url = text.startswith(("http://", "https://", "//", "/"))
    if not (looks_like_url or _IMAGE_EXT_RE.search(text)):
        return None
    return urljoin(url, text)


def _from_json_ld(soup: BeautifulSoup, _url: str) -> dict[str, Any] | None:
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            # Malformed JSON-LD is common; skip it rather than failing the page.
            continue
        product = _find_product(payload)
        if product is None:
            continue
        offer = _first(product.get("offers"))
        rating = product.get("aggregateRating") or {}
        return {
            "name": _text(product.get("name")),
            "price": _to_float(offer.get("price") if offer else None),
            "currency": _text(offer.get("priceCurrency") if offer else None),
            "availability": _strip_schema_url(offer.get("availability") if offer else None),
            "brand": _brand_name(product.get("brand")),
            "sku": _text(product.get("sku") or product.get("mpn")),
            "image": _first_scalar(product.get("image")),
            "rating": _to_float(rating.get("ratingValue") if isinstance(rating, dict) else None),
            "review_count": _to_int(
                rating.get("reviewCount") or rating.get("ratingCount")
                if isinstance(rating, dict)
                else None
            ),
            "description": _text(product.get("description")),
            "source": "json-ld",
        }
    return None


def _from_opengraph(soup: BeautifulSoup, _url: str) -> dict[str, Any] | None:
    meta = _meta_map(soup)
    price = meta.get("product:price:amount") or meta.get("og:price:amount")
    title = meta.get("og:title")
    if not (price or (title and meta.get("og:type") == "product")):
        return None
    return {
        "name": title,
        "price": _to_float(price),
        "currency": meta.get("product:price:currency") or meta.get("og:price:currency"),
        "availability": meta.get("product:availability") or meta.get("og:availability"),
        "brand": meta.get("product:brand"),
        "sku": meta.get("product:retailer_item_id"),
        "image": meta.get("og:image"),
        "description": meta.get("og:description"),
        "source": "opengraph",
    }


def _from_microdata(soup: BeautifulSoup, _url: str) -> dict[str, Any] | None:
    scope = soup.find(attrs={"itemtype": re.compile(r"schema\.org/Product", re.I)})
    if not isinstance(scope, Tag):
        return None

    def prop(name: str, *, url_valued: bool = False) -> str | None:
        node = scope.find(attrs={"itemprop": name})
        if not isinstance(node, Tag):
            return None
        # Only URL-valued props read src/href. Doing it for text props turns
        # <a itemprop="brand" href="/facets/brands/Nutella">Nutella</a> into the href.
        attributes = ("content", "src", "href") if url_valued else ("content",)
        for attribute in attributes:
            candidate = node.get(attribute)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return node.get_text(strip=True) or None

    return {
        "name": prop("name"),
        "price": _to_float(prop("price")),
        "currency": prop("priceCurrency"),
        "availability": _strip_schema_url(prop("availability")),
        "brand": prop("brand"),
        "sku": prop("sku"),
        "image": prop("image", url_valued=True),
        "description": prop("description"),
        "source": "microdata",
    }


def _meta_map(soup: BeautifulSoup) -> dict[str, str]:
    found: dict[str, str] = {}
    for tag in soup.find_all("meta"):
        if not isinstance(tag, Tag):
            continue
        key = tag.get("property") or tag.get("name")
        content = tag.get("content")
        if isinstance(key, str) and isinstance(content, str):
            found.setdefault(key.lower(), content.strip())
    return found


def _find_product(payload: Any) -> dict[str, Any] | None:
    """Locate a Product entity, including inside @graph wrappers and arrays."""
    if isinstance(payload, list):
        for item in payload:
            found = _find_product(item)
            if found is not None:
                return found
        return None
    if not isinstance(payload, dict):
        return None
    if _is_product(payload.get("@type")):
        return payload
    if "@graph" in payload:
        return _find_product(payload["@graph"])
    return None


def _is_product(type_value: Any) -> bool:
    if isinstance(type_value, str):
        return type_value.lower().endswith("product")
    if isinstance(type_value, list):
        return any(_is_product(t) for t in type_value)
    return False


def _first(value: Any) -> dict[str, Any] | None:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                return item
        return None
    return value if isinstance(value, dict) else None


def _first_scalar(value: Any) -> str | None:
    if isinstance(value, list):
        for item in value:
            scalar = _first_scalar(item)
            if scalar:
                return scalar
        return None
    if isinstance(value, dict):
        return _first_scalar(value.get("url") or value.get("contentUrl"))
    return _text(value)


def _brand_name(value: Any) -> str | None:
    if isinstance(value, dict):
        return _text(value.get("name"))
    if isinstance(value, list):
        return _brand_name(value[0]) if value else None
    return _text(value)


def _strip_schema_url(value: Any) -> str | None:
    text = _text(value)
    if text is None:
        return None
    return text.rsplit("/", 1)[-1] if text.startswith("http") else text


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    return text or None


def _to_float(value: Any) -> float | None:
    text = _text(value)
    if text is None:
        return None
    cleaned = _PRICE_CLEAN_RE.sub("", text).replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    return int(number) if number is not None else None
