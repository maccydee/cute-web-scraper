"""Environment-driven configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass

_DEFAULT_DELAY_MS = 1000
_DEFAULT_MAX_CONCURRENT = 5
_DEFAULT_CACHE_TTL_S = 300
_DEFAULT_CACHE_MAX_ENTRIES = 500


@dataclass(frozen=True)
class Config:
    """Runtime configuration, all sourced from the environment."""

    delay_ms: int
    """(MEASURED, milliseconds) Base delay between requests to the same domain."""

    max_concurrent: int
    """Maximum parallel in-flight requests across all domains."""

    auth_token: str | None
    """Bearer token for HTTP mode. None disables auth (loopback-only bind)."""

    user_data_dir: str | None
    """Chrome profile directory. When set, Playwright inherits its logged-in sessions."""

    cache_ttl_s: int
    """(MEASURED, seconds) How long a fetched page stays reusable."""

    cache_max_entries: int
    """Maximum cached pages before least-recently-used eviction."""

    @classmethod
    def from_env(cls) -> Config:
        delay_ms = _int_env("SCRAPER_DELAY_MS", _DEFAULT_DELAY_MS, minimum=0)
        max_concurrent = _int_env("SCRAPER_MAX_CONCURRENT", _DEFAULT_MAX_CONCURRENT, minimum=1)
        cache_ttl_s = _int_env("SCRAPER_CACHE_TTL_S", _DEFAULT_CACHE_TTL_S, minimum=0)
        cache_max_entries = _int_env(
            "SCRAPER_CACHE_MAX_ENTRIES", _DEFAULT_CACHE_MAX_ENTRIES, minimum=1
        )
        return cls(
            delay_ms=delay_ms,
            max_concurrent=max_concurrent,
            auth_token=_str_env("SCRAPER_AUTH_TOKEN"),
            user_data_dir=_str_env("SCRAPER_CHROME_USER_DATA_DIR"),
            cache_ttl_s=cache_ttl_s,
            cache_max_entries=cache_max_entries,
        )


def _str_env(name: str) -> str | None:
    raw = os.environ.get(name, "").strip()
    return raw or None


def _int_env(name: str, default: int, *, minimum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got: {raw!r}") from None
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got: {value}")
    return value
