import asyncio

import pytest

from cute_web_scraper.config import Config
from cute_web_scraper.rate_limiter import DomainRateLimiter


def _config(delay_ms: int) -> Config:
    return Config(
        delay_ms=delay_ms,
        max_concurrent=5,
        auth_token=None,
        user_data_dir=None,
        cache_ttl_s=0,
        cache_max_entries=10,
    )


@pytest.fixture
def limiter():
    return DomainRateLimiter(_config(100))


async def test_same_domain_second_request_waits(limiter):
    loop = asyncio.get_running_loop()
    await limiter.wait("https://example.com/a")
    t0 = loop.time()
    await limiter.wait("https://example.com/b")
    assert loop.time() - t0 >= 0.09


async def test_first_request_does_not_wait(limiter):
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await limiter.wait("https://fresh.example/a")
    assert loop.time() - t0 < 0.05


async def test_different_domains_run_concurrently(limiter):
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await asyncio.gather(limiter.wait("https://a.com/p"), limiter.wait("https://b.com/p"))
    assert loop.time() - t0 < 0.05


def test_block_doubles_delay(limiter):
    limiter.record_block("https://example.com/")
    assert limiter.current_delay("https://example.com/") == pytest.approx(0.2)


def test_delay_capped(limiter):
    for _ in range(40):
        limiter.record_block("https://example.com/")
    assert limiter.current_delay("https://example.com/") == 60.0


def test_success_decays_but_not_below_base(limiter):
    limiter.record_block("https://example.com/")
    for _ in range(10):
        limiter.record_success("https://example.com/")
    delay = limiter.current_delay("https://example.com/")
    assert delay < 0.2
    assert delay >= 0.1


def test_zero_base_delay_still_backs_off():
    """SCRAPER_DELAY_MS=0 disables the polite delay but must NOT disable backoff."""
    limiter = DomainRateLimiter(_config(0))
    assert limiter.current_delay("https://example.com/") == 0.0
    limiter.record_block("https://example.com/")
    assert limiter.current_delay("https://example.com/") == pytest.approx(1.0)


def test_zero_base_delay_recovers_to_zero():
    limiter = DomainRateLimiter(_config(0))
    limiter.record_block("https://example.com/")
    for _ in range(200):
        limiter.record_success("https://example.com/")
    assert limiter.current_delay("https://example.com/") == pytest.approx(0.0, abs=1e-6)


def test_domain_state_is_bounded():
    limiter = DomainRateLimiter(_config(0), max_domains=10)
    for i in range(50):
        limiter.record_block(f"https://site{i}.com/")
    assert limiter.tracked_domains() <= 10
