import pytest
from starlette.testclient import TestClient

from cute_web_scraper._http import build_app, check_bind_is_safe
from cute_web_scraper.config import Config
from cute_web_scraper.server import ScraperHolder, create_server


def _config(auth_token: str | None) -> Config:
    return Config(
        delay_ms=0,
        max_concurrent=5,
        auth_token=auth_token,
        user_data_dir=None,
        cache_ttl_s=0,
        cache_max_entries=10,
    )


def _app(auth_token: str | None):
    return build_app(_config(auth_token), create_server(ScraperHolder()))


def test_mcp_and_health_routes_exist():
    """v1 never asserted this; its SSE wiring was wrong and untested."""
    paths = {getattr(r, "path", None) for r in _app(None).routes}
    assert "/mcp" in paths
    assert "/health" in paths


def test_health_open_without_token():
    with TestClient(_app(None)) as client:
        assert client.get("/health").status_code == 200


def test_missing_token_rejected():
    with TestClient(_app("mysecret")) as client:
        assert client.get("/health").status_code == 401


def test_wrong_token_rejected():
    with TestClient(_app("mysecret")) as client:
        assert client.get("/health", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_correct_token_accepted():
    with TestClient(_app("mysecret")) as client:
        resp = client.get("/health", headers={"Authorization": "Bearer mysecret"})
        assert resp.status_code == 200


def test_bearer_scheme_is_case_insensitive():
    with TestClient(_app("mysecret")) as client:
        resp = client.get("/health", headers={"Authorization": "bearer mysecret"})
        assert resp.status_code == 200


def test_non_loopback_bind_requires_a_token():
    with pytest.raises(ValueError, match="SCRAPER_AUTH_TOKEN"):
        check_bind_is_safe("0.0.0.0", _config(None))


def test_non_loopback_bind_allowed_with_token():
    check_bind_is_safe("0.0.0.0", _config("mysecret"))


def test_loopback_bind_needs_no_token():
    check_bind_is_safe("127.0.0.1", _config(None))
    check_bind_is_safe("localhost", _config(None))
