"""Place and local-business lookup via OpenStreetMap.

Google Maps was the obvious target here and it does not work: Google serves an
automated browser a consent interstitial and then, once that is cleared, a degraded
map shell with no place panel. Stealth flags did not change that. Beating it needs
residential proxies and challenge solving, which this tool deliberately does not do.

OpenStreetMap gives the same fields through documented, open endpoints with no key
and no blocking — name, address, coordinates, phone, website, opening hours and
category. The one thing it has no equivalent for is star ratings and review counts,
which are Google's own proprietary data.

Two endpoints are used:
  * Nominatim  -- free-text search ("the British Museum", "cafes in Shoreditch")
  * Overpass   -- everything of a category within a radius ("dentists near Bath")

Both are volunteer-run. Nominatim's usage policy caps clients at one request per
second and requires an identifying User-Agent, so this module enforces its own
minimum interval rather than trusting the configured scrape delay.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

_OVERPASS_MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
"""The main Overpass instance is volunteer-run and regularly returns 504 under load
(observed in testing). These are the established public mirrors; a query falls
through them in order rather than failing on the first busy server."""

_RETRYABLE_STATUSES = {429, 502, 503, 504}
_USER_AGENT = "cute-web-scraper/0.1 (+https://github.com/maccydee/cute-web-scraper)"
_TIMEOUT_S = 60.0

_NOMINATIM_MIN_INTERVAL_S = 1.1
"""Nominatim's published policy is one request per second. Enforced here so that
setting SCRAPER_DELAY_MS=0 cannot turn this tool into a policy violation."""

_MAX_LIMIT = 200

_CATEGORY_TAGS: dict[str, tuple[str, str]] = {
    "restaurant": ("amenity", "restaurant"),
    "cafe": ("amenity", "cafe"),
    "coffee": ("amenity", "cafe"),
    "pub": ("amenity", "pub"),
    "bar": ("amenity", "bar"),
    "hotel": ("tourism", "hotel"),
    "dentist": ("amenity", "dentist"),
    "doctor": ("amenity", "doctors"),
    "pharmacy": ("amenity", "pharmacy"),
    "veterinary": ("amenity", "veterinary"),
    "vet": ("amenity", "veterinary"),
    "school": ("amenity", "school"),
    "bank": ("amenity", "bank"),
    "gym": ("leisure", "fitness_centre"),
    "hairdresser": ("shop", "hairdresser"),
    "barber": ("shop", "hairdresser"),
    "bakery": ("shop", "bakery"),
    "butcher": ("shop", "butcher"),
    "supermarket": ("shop", "supermarket"),
    "florist": ("shop", "florist"),
    "car_repair": ("shop", "car_repair"),
    "garage": ("shop", "car_repair"),
    "estate_agent": ("office", "estate_agent"),
    "solicitor": ("office", "lawyer"),
    "lawyer": ("office", "lawyer"),
    "accountant": ("office", "accountant"),
    "museum": ("tourism", "museum"),
}

_nominatim_lock = asyncio.Lock()
_nominatim_last_call: float = 0.0


class PlacesError(Exception):
    """Raised when a place lookup cannot be completed."""


def category_to_tag(category: str) -> tuple[str, str]:
    """Map a friendly category to an OSM key/value, or accept a raw `key=value`."""
    text = (category or "").strip().lower().replace(" ", "_")
    if not text:
        raise PlacesError("Provide a category, for example 'cafe' or 'amenity=dentist'.")
    if "=" in text:
        key, _, value = text.partition("=")
        if key and value:
            return key, value
    if text in _CATEGORY_TAGS:
        return _CATEGORY_TAGS[text]
    raise PlacesError(
        f"Unknown category {category!r}. Use one of: "
        + ", ".join(sorted(_CATEGORY_TAGS)[:12])
        + ", ... or a raw OSM tag such as 'amenity=dentist'. "
        "See https://wiki.openstreetmap.org/wiki/Map_features for the full list."
    )


async def search_places(
    query: str, client: httpx.AsyncClient, *, limit: int = 20
) -> list[dict[str, Any]]:
    """Free-text place search."""
    if not (query or "").strip():
        raise PlacesError("Provide something to search for.")
    payload = await _nominatim(
        client,
        {
            "q": query,
            "format": "jsonv2",
            "addressdetails": "1",
            "extratags": "1",
            "limit": str(min(max(limit, 1), 50)),
        },
    )
    return [_from_nominatim(item) for item in payload]


async def geocode_one(query: str, client: httpx.AsyncClient) -> tuple[float, float, str]:
    """Resolve a place name to coordinates, for use as the centre of a radius search."""
    payload = await _nominatim(
        client, {"q": query, "format": "jsonv2", "limit": "1", "addressdetails": "1"}
    )
    if not payload:
        raise PlacesError(f"Could not find a location called {query!r}.")
    first = payload[0]
    try:
        return float(first["lat"]), float(first["lon"]), str(first.get("display_name", query))
    except (KeyError, TypeError, ValueError):
        raise PlacesError(f"Geocoding returned no usable coordinates for {query!r}.") from None


async def find_nearby(
    category: str,
    lat: float,
    lon: float,
    client: httpx.AsyncClient,
    *,
    radius_m: int = 5000,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Everything of a category within a radius, via Overpass."""
    key, value = category_to_tag(category)
    capped = min(max(limit, 1), _MAX_LIMIT)
    # `around` is used rather than an admin-boundary area lookup: boundary names are
    # inconsistent across countries and silently return zero results when they miss.
    query = (
        "[out:json][timeout:50];("
        f'node["{key}"="{value}"](around:{radius_m},{lat},{lon});'
        f'way["{key}"="{value}"](around:{radius_m},{lat},{lon});'
        f");out center tags {capped};"
    )
    payload = await _overpass(query, client)
    return [_from_overpass(e) for e in payload.get("elements", [])]


async def _overpass(query: str, client: httpx.AsyncClient) -> dict[str, Any]:
    """POST to each mirror in turn, moving on when one is busy or unreachable."""
    problems: list[str] = []
    for mirror in _OVERPASS_MIRRORS:
        try:
            response = await client.post(
                mirror,
                data={"data": query},
                headers={"User-Agent": _USER_AGENT},
                timeout=_TIMEOUT_S,
            )
        except httpx.HTTPError as exc:
            problems.append(f"{_host(mirror)}: {type(exc).__name__}")
            continue
        if response.status_code in _RETRYABLE_STATUSES:
            problems.append(f"{_host(mirror)}: HTTP {response.status_code}")
            log.info(
                "Overpass mirror %s busy (%d); trying next", _host(mirror), response.status_code
            )
            continue
        if response.status_code != 200:
            raise PlacesError(f"Overpass returned HTTP {response.status_code} from {_host(mirror)}")
        try:
            payload = response.json()
        except ValueError:
            problems.append(f"{_host(mirror)}: non-JSON response")
            continue
        return payload if isinstance(payload, dict) else {}
    raise PlacesError(
        "Every Overpass mirror was unavailable ("
        + "; ".join(problems)
        + "). These are volunteer-run services; try again shortly, "
        "or narrow the radius to make the query cheaper."
    )


def _host(url: str) -> str:
    return url.split("/")[2]


async def _nominatim(client: httpx.AsyncClient, params: dict[str, str]) -> list[dict[str, Any]]:
    global _nominatim_last_call
    async with _nominatim_lock:
        loop = asyncio.get_running_loop()
        gap = _NOMINATIM_MIN_INTERVAL_S - (loop.time() - _nominatim_last_call)
        if gap > 0:
            await asyncio.sleep(gap)
        try:
            response = await client.get(
                _NOMINATIM_URL,
                params=params,
                headers={"User-Agent": _USER_AGENT},
                timeout=_TIMEOUT_S,
            )
        except httpx.HTTPError as exc:
            raise PlacesError(f"Nominatim request failed: {exc}") from None
        finally:
            _nominatim_last_call = loop.time()

    if response.status_code != 200:
        raise PlacesError(f"Nominatim returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        raise PlacesError("Nominatim did not return JSON.") from None
    return payload if isinstance(payload, list) else []


def _from_nominatim(item: dict[str, Any]) -> dict[str, Any]:
    extra = item.get("extratags") or {}
    return {
        "name": item.get("name") or _first_line(item.get("display_name")),
        "category": _join(item.get("category"), item.get("type")),
        "address": item.get("display_name"),
        "lat": _to_float(item.get("lat")),
        "lon": _to_float(item.get("lon")),
        "phone": extra.get("phone") or extra.get("contact:phone"),
        "website": extra.get("website") or extra.get("contact:website"),
        "opening_hours": extra.get("opening_hours"),
        "osm_type": item.get("osm_type"),
        "osm_id": item.get("osm_id"),
        "source": "nominatim",
    }


def _from_overpass(element: dict[str, Any]) -> dict[str, Any]:
    tags = element.get("tags") or {}
    centre = element.get("center") or {}
    return {
        "name": tags.get("name"),
        "category": _join(*_element_category(tags)),
        "address": _compose_address(tags),
        "lat": _to_float(element.get("lat", centre.get("lat"))),
        "lon": _to_float(element.get("lon", centre.get("lon"))),
        "phone": tags.get("phone") or tags.get("contact:phone"),
        "website": tags.get("website") or tags.get("contact:website"),
        "opening_hours": tags.get("opening_hours"),
        "osm_type": element.get("type"),
        "osm_id": element.get("id"),
        "source": "overpass",
    }


def _element_category(tags: dict[str, Any]) -> tuple[Any, Any]:
    for key in ("amenity", "shop", "tourism", "office", "leisure", "healthcare"):
        if key in tags:
            return key, tags[key]
    return None, None


def _compose_address(tags: dict[str, Any]) -> str | None:
    parts = [
        _join_space(tags.get("addr:housenumber"), tags.get("addr:street")),
        tags.get("addr:city"),
        tags.get("addr:postcode"),
    ]
    present = [str(p) for p in parts if p]
    return ", ".join(present) if present else None


def _join(key: Any, value: Any) -> str | None:
    if key and value:
        return f"{key}/{value}"
    return str(value or key) if (value or key) else None


def _join_space(a: Any, b: Any) -> str | None:
    present = [str(x) for x in (a, b) if x]
    return " ".join(present) if present else None


def _first_line(value: Any) -> str | None:
    if not value:
        return None
    return str(value).split(",")[0].strip() or None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
