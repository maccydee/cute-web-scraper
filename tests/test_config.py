import pytest

from cute_web_scraper.config import Config

_ALL = [
    "SCRAPER_DELAY_MS",
    "SCRAPER_MAX_CONCURRENT",
    "SCRAPER_AUTH_TOKEN",
    "SCRAPER_CHROME_USER_DATA_DIR",
    "SCRAPER_CACHE_TTL_S",
]


@pytest.fixture
def clean_env(monkeypatch):
    for name in _ALL:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_defaults(clean_env):
    c = Config.from_env()
    assert c.delay_ms == 1000
    assert c.max_concurrent == 5
    assert c.auth_token is None
    assert c.user_data_dir is None
    assert c.cache_ttl_s == 300


def test_custom_values(clean_env):
    clean_env.setenv("SCRAPER_DELAY_MS", "500")
    clean_env.setenv("SCRAPER_MAX_CONCURRENT", "10")
    clean_env.setenv("SCRAPER_AUTH_TOKEN", "secret")
    clean_env.setenv("SCRAPER_CHROME_USER_DATA_DIR", "/tmp/profile")
    c = Config.from_env()
    assert c.delay_ms == 500
    assert c.max_concurrent == 10
    assert c.auth_token == "secret"
    assert c.user_data_dir == "/tmp/profile"


def test_zero_delay_allowed(clean_env):
    clean_env.setenv("SCRAPER_DELAY_MS", "0")
    assert Config.from_env().delay_ms == 0


def test_negative_delay_rejected(clean_env):
    clean_env.setenv("SCRAPER_DELAY_MS", "-5")
    with pytest.raises(ValueError, match="SCRAPER_DELAY_MS"):
        Config.from_env()


def test_invalid_max_concurrent(clean_env):
    clean_env.setenv("SCRAPER_MAX_CONCURRENT", "0")
    with pytest.raises(ValueError, match="SCRAPER_MAX_CONCURRENT"):
        Config.from_env()


def test_non_integer_delay(clean_env):
    clean_env.setenv("SCRAPER_DELAY_MS", "abc")
    with pytest.raises(ValueError, match="SCRAPER_DELAY_MS"):
        Config.from_env()


def test_blank_auth_token_is_none(clean_env):
    clean_env.setenv("SCRAPER_AUTH_TOKEN", "   ")
    assert Config.from_env().auth_token is None
