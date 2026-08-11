import json
from unittest.mock import AsyncMock

import httpx
import pytest

from cute_web_scraper.config import Config
from cute_web_scraper.server import ScraperHolder, create_server
from cute_web_scraper.store import ResultStore

NOMINATIM_HIT = [
    {
        "name": "British Museum",
        "display_name": "British Museum, Great Russell Street, London",
        "lat": "51.5193118",
        "lon": "-0.1267051",
        "category": "tourism",
        "type": "museum",
        "osm_type": "way",
        "osm_id": 1,
        "extratags": {"phone": "+44 20 7323 8299", "website": "https://britishmuseum.org"},
    }
]

OVERPASS_HIT = {
    "elements": [
        {
            "type": "node",
            "id": 1,
            "lat": 51.38,
            "lon": -2.36,
            "tags": {"name": "Green Park Dental", "amenity": "dentist", "phone": "+44 1"},
        },
        # Unnamed features are noise in a lead list and must be filtered out.
        {"type": "node", "id": 2, "lat": 51.39, "lon": -2.37, "tags": {"amenity": "dentist"}},
    ]
}

ROWS = [
    {"vendor": "A", "name": "x", "price": 10.0},
    {"vendor": "A", "name": "x", "price": 10.0},
    {"vendor": "B", "name": "y", "price": None},
]


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
        max_inline_chars=50_000,
    )
    scraper = AsyncMock()
    scraper.http = httpx.AsyncClient()
    h = ScraperHolder()
    h.set(scraper, store=store, config=config)
    return h


@pytest.fixture
def mcp(holder):
    return create_server(holder)


async def _json(mcp, name: str, args: dict):
    result = await mcp.call_tool(name, args)
    return json.loads(result.content[0].text)


# ------------------------------------------------------- query_table(save_as=...)


async def test_query_save_as_creates_new_table(mcp, store):
    store.save("src", ROWS)
    payload = await _json(
        mcp,
        "query_table",
        {"sql": "SELECT DISTINCT vendor, name, price FROM src", "save_as": "clean"},
    )
    assert payload["saved_to"] == "clean"
    assert payload["row_count"] == 2  # the duplicate row collapsed
    assert payload["replaced_existing_table"] is False
    assert store.get_table("src").row_count == 3  # source untouched


async def test_query_save_as_merges_and_renames_columns(mcp, store):
    """SQL already expresses merge_columns and update_column."""
    store.save("src", [{"street": "12 Green Park", "city": "Bath", "postcode": "BA1 1JB"}])
    payload = await _json(
        mcp,
        "query_table",
        {
            "sql": "SELECT street || ', ' || city || ', ' || postcode AS address FROM src",
            "save_as": "addresses",
        },
    )
    assert payload["columns"] == ["address"]
    assert payload["sample"][0]["address"] == "12 Green Park, Bath, BA1 1JB"


async def test_query_save_as_can_drop_rows(mcp, store):
    store.save("src", ROWS)
    payload = await _json(
        mcp,
        "query_table",
        {"sql": "SELECT * FROM src WHERE price IS NOT NULL", "save_as": "priced"},
    )
    assert payload["row_count"] == 2


async def test_overwriting_the_source_is_reported(mcp, store):
    """A filter that targets its own source is destructive; that must be visible."""
    store.save("src", ROWS)
    payload = await _json(
        mcp, "query_table", {"sql": "SELECT * FROM src WHERE price IS NOT NULL", "save_as": "src"}
    )
    assert payload["replaced_existing_table"] is True
    assert payload["row_count"] == 2
    assert store.get_table("src").row_count == 2


async def test_query_save_as_rejects_writes(mcp, store):
    store.save("src", ROWS)
    payload = await _json(mcp, "query_table", {"sql": "DELETE FROM src", "save_as": "x"})
    assert "error" in payload
    assert store.get_table("src").row_count == 3


async def test_query_save_as_with_no_rows_errors(mcp, store):
    store.save("src", ROWS)
    payload = await _json(
        mcp, "query_table", {"sql": "SELECT * FROM src WHERE vendor = 'ZZZ'", "save_as": "empty"}
    )
    assert "error" in payload
    assert "nothing to save" in payload["error"]


async def test_query_save_as_invalid_name_errors(mcp, store):
    store.save("src", ROWS)
    payload = await _json(mcp, "query_table", {"sql": "SELECT * FROM src", "save_as": "bad name"})
    assert "error" in payload


# ----------------------------------------------------------------- place tools


async def test_find_places(mcp, holder, httpx_mock):
    httpx_mock.add_response(json=NOMINATIM_HIT)
    payload = await _json(mcp, "find_places", {"query": "British Museum"})
    await holder.require().http.aclose()
    assert payload["count"] == 1
    assert payload["results"][0]["name"] == "British Museum"
    assert payload["results"][0]["phone"] == "+44 20 7323 8299"


async def test_find_places_save_as(mcp, holder, httpx_mock):
    httpx_mock.add_response(json=NOMINATIM_HIT)
    payload = await _json(mcp, "find_places", {"query": "British Museum", "save_as": "places"})
    await holder.require().http.aclose()
    assert payload["saved_to"] == "places"
    assert payload["row_count"] == 1


async def test_find_places_nearby_filters_unnamed(mcp, holder, httpx_mock):
    httpx_mock.add_response(json=NOMINATIM_HIT)  # geocode
    httpx_mock.add_response(json=OVERPASS_HIT)  # overpass
    payload = await _json(mcp, "find_places_nearby", {"category": "dentist", "near": "Bath"})
    await holder.require().http.aclose()
    assert payload["count"] == 1
    assert payload["results"][0]["name"] == "Green Park Dental"
    assert payload["radius_m"] == 5000


async def test_find_places_nearby_save_as_keeps_centre(mcp, holder, httpx_mock):
    httpx_mock.add_response(json=NOMINATIM_HIT)
    httpx_mock.add_response(json=OVERPASS_HIT)
    payload = await _json(
        mcp, "find_places_nearby", {"category": "dentist", "near": "Bath", "save_as": "leads"}
    )
    await holder.require().http.aclose()
    assert payload["saved_to"] == "leads"
    assert payload["row_count"] == 1
    assert "British Museum" in payload["centre"]


async def test_find_places_nearby_unknown_category(mcp, holder, httpx_mock):
    httpx_mock.add_response(json=NOMINATIM_HIT)
    payload = await _json(
        mcp, "find_places_nearby", {"category": "wizard emporium", "near": "Bath"}
    )
    await holder.require().http.aclose()
    assert "error" in payload
    assert "Unknown category" in payload["error"]


async def test_find_places_handles_upstream_failure(mcp, holder, httpx_mock):
    httpx_mock.add_response(status_code=503)
    payload = await _json(mcp, "find_places", {"query": "anything"})
    await holder.require().http.aclose()
    assert "error" in payload
    assert payload["results"] == []
