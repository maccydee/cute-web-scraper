import httpx
import pytest

from cute_web_scraper.places import (
    PlacesError,
    category_to_tag,
    find_nearby,
    geocode_one,
    search_places,
)

NOMINATIM_HIT = [
    {
        "name": "British Museum",
        "display_name": "British Museum, Great Russell Street, London, WC1B 3DG",
        "lat": "51.5193118",
        "lon": "-0.1267051",
        "category": "tourism",
        "type": "museum",
        "osm_type": "way",
        "osm_id": 1234,
        "extratags": {
            "phone": "+44 20 7323 8299",
            "website": "https://www.britishmuseum.org",
            "opening_hours": "Sa-Th 10:00-17:00; Fr 10:00-20:30",
        },
    }
]

OVERPASS_HIT = {
    "elements": [
        {
            "type": "node",
            "id": 99,
            "lat": 51.38,
            "lon": -2.36,
            "tags": {
                "name": "Green Park Dental",
                "amenity": "dentist",
                "phone": "+44 1225 000000",
                "website": "https://example.dental",
                "addr:housenumber": "12",
                "addr:street": "Green Park",
                "addr:city": "Bath",
                "addr:postcode": "BA1 1JB",
            },
        },
        {
            "type": "way",
            "id": 100,
            "center": {"lat": 51.39, "lon": -2.37},
            "tags": {"name": "Way Dental", "amenity": "dentist"},
        },
    ]
}


# ------------------------------------------------------------------- categories


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        ("cafe", ("amenity", "cafe")),
        ("Dentist", ("amenity", "dentist")),
        ("car repair", ("shop", "car_repair")),
        ("amenity=pharmacy", ("amenity", "pharmacy")),
        ("shop=bakery", ("shop", "bakery")),
    ],
)
def test_category_mapping(category, expected):
    assert category_to_tag(category) == expected


def test_unknown_category_lists_options():
    with pytest.raises(PlacesError, match="Unknown category"):
        category_to_tag("wizard emporium")


def test_empty_category_rejected():
    with pytest.raises(PlacesError):
        category_to_tag("")


# ----------------------------------------------------------------------- search


async def test_search_places_maps_fields(httpx_mock):
    httpx_mock.add_response(json=NOMINATIM_HIT)
    async with httpx.AsyncClient() as client:
        places = await search_places("British Museum", client)
    p = places[0]
    assert p["name"] == "British Museum"
    assert p["phone"] == "+44 20 7323 8299"
    assert p["website"] == "https://www.britishmuseum.org"
    assert p["opening_hours"].startswith("Sa-Th")
    assert p["category"] == "tourism/museum"
    assert p["lat"] == pytest.approx(51.5193118)
    assert p["source"] == "nominatim"


async def test_search_places_empty_query_rejected():
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlacesError):
            await search_places("   ", client)


async def test_search_places_handles_no_results(httpx_mock):
    httpx_mock.add_response(json=[])
    async with httpx.AsyncClient() as client:
        assert await search_places("nowhere at all", client) == []


async def test_nominatim_http_error_raises(httpx_mock):
    httpx_mock.add_response(status_code=503)
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlacesError, match="503"):
            await search_places("x", client)


# ---------------------------------------------------------------------- geocode


async def test_geocode_one(httpx_mock):
    httpx_mock.add_response(json=NOMINATIM_HIT)
    async with httpx.AsyncClient() as client:
        lat, lon, label = await geocode_one("British Museum", client)
    assert lat == pytest.approx(51.5193118)
    assert lon == pytest.approx(-0.1267051)
    assert "British Museum" in label


async def test_geocode_unknown_place_raises(httpx_mock):
    httpx_mock.add_response(json=[])
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlacesError, match="Could not find"):
            await geocode_one("qqqzzz", client)


# ----------------------------------------------------------------------- nearby


async def test_find_nearby_maps_nodes_and_ways(httpx_mock):
    httpx_mock.add_response(json=OVERPASS_HIT)
    async with httpx.AsyncClient() as client:
        places = await find_nearby("dentist", 51.38, -2.36, client, radius_m=4000)
    assert len(places) == 2
    first = places[0]
    assert first["name"] == "Green Park Dental"
    assert first["category"] == "amenity/dentist"
    assert first["address"] == "12 Green Park, Bath, BA1 1JB"
    assert first["phone"] == "+44 1225 000000"
    # A `way` has no lat/lon of its own; its centre must be used instead.
    assert places[1]["lat"] == pytest.approx(51.39)


async def test_find_nearby_builds_a_radius_query(httpx_mock):
    httpx_mock.add_response(json={"elements": []})
    async with httpx.AsyncClient() as client:
        await find_nearby("cafe", 51.5, -0.1, client, radius_m=1234, limit=7)
    body = httpx_mock.get_requests()[0].content.decode()
    assert "around%3A1234%2C51.5%2C-0.1" in body or "around:1234,51.5,-0.1" in body
    assert "amenity" in body


async def test_find_nearby_rejects_unknown_category():
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlacesError, match="Unknown category"):
            await find_nearby("wizard emporium", 51.5, -0.1, client)


async def test_busy_mirror_falls_through_to_the_next(httpx_mock):
    """Observed live: the main Overpass instance returns 504 under load."""
    httpx_mock.add_response(status_code=504)
    httpx_mock.add_response(json=OVERPASS_HIT)
    async with httpx.AsyncClient() as client:
        places = await find_nearby("dentist", 51.38, -2.36, client)
    assert len(places) == 2
    assert len(httpx_mock.get_requests()) == 2


async def test_all_mirrors_busy_reports_each(httpx_mock):
    for status in (504, 429, 502):
        httpx_mock.add_response(status_code=status)
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlacesError, match="Every Overpass mirror"):
            await find_nearby("cafe", 51.5, -0.1, client)


async def test_non_retryable_status_raises_immediately(httpx_mock):
    httpx_mock.add_response(status_code=400)
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlacesError, match="400"):
            await find_nearby("cafe", 51.5, -0.1, client)
    assert len(httpx_mock.get_requests()) == 1
