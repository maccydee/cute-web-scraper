"""End-to-end checks against a stable, scraping-friendly public site.

pytest -m integration tests/integration/test_smoke.py -v -s
"""

from __future__ import annotations

import time

import pytest
import pytest_asyncio

from cute_web_scraper.config import Config
from cute_web_scraper.extractors import extract_links
from cute_web_scraper.scraper import Scraper

pytestmark = pytest.mark.integration

BASE = "https://books.toscrape.com/"
SECOND = "https://books.toscrape.com/catalogue/page-2.html"
DELAY_MS = 500


def _config() -> Config:
    return Config(
        delay_ms=DELAY_MS,
        max_concurrent=3,
        auth_token=None,
        user_data_dir=None,
        cache_ttl_s=0,
        cache_max_entries=50,
    )


@pytest_asyncio.fixture
async def scraper():
    async with Scraper(_config()) as s:
        yield s


async def test_static_fetch(scraper):
    result = await scraper.fetch(BASE)
    assert result.status_code == 200
    assert result.blocked is False
    assert len(result.markdown) > 500
    assert result.title
    print(f"\ntitle={result.title!r} links={result.links_count} md={len(result.markdown)}b")


async def test_links_are_absolute(scraper):
    result = await scraper.fetch(BASE)
    links = extract_links(result.html, result.url)
    assert len(links) >= 10
    assert all(link["value"].startswith("http") for link in links)
    print(f"\n{len(links)} absolute links, first: {links[0]['value']}")


async def test_crawl_discovers_pages(scraper):
    from cute_web_scraper.crawler import crawl_by_links, discover_sitemap_urls

    urls = await discover_sitemap_urls(BASE, scraper.http)
    source = "sitemap"
    if not urls:
        urls = await crawl_by_links(BASE, limit=5, scraper=scraper)
        source = "links"
    assert len(urls) >= 1
    print(f"\ndiscovered {len(urls)} urls via {source}, first: {urls[0]}")


async def test_rate_limiter_spaces_same_domain_requests(scraper):
    """Back-to-back limiter calls on one domain are spaced by the configured delay.

    Timed around rate_limiter.wait() rather than two fetches. The limiter is
    start-to-start, so when a host answers in ~3.5s and the delay is 500ms the network
    time absorbs the interval entirely: two fetches take about the same wall clock
    whether the limiter runs or not, which makes a fetch-timed assertion unfalsifiable.
    Wiring into Scraper.fetch is covered deterministically in tests/test_scraper_static.py.
    """
    start = time.monotonic()
    await scraper.rate_limiter.wait(BASE)
    await scraper.rate_limiter.wait(SECOND)  # same domain as BASE
    elapsed = time.monotonic() - start  # (MEASURED, seconds) limiter only
    minimum = DELAY_MS / 1000 * 0.9
    assert elapsed >= minimum, (
        f"two same-domain limiter calls took {elapsed:.2f}s, below the {DELAY_MS}ms rate"
    )
    print(f"\nlimiter spaced two same-domain calls by {elapsed:.2f}s (floor {minimum:.2f}s)")


async def test_different_domains_are_not_penalised(scraper):
    """A fresh domain incurs no waiting from the limiter.

    Timed around rate_limiter.wait() rather than a whole fetch. A fetch also includes
    network latency, and these hosts answer a cold request in ~3.5s, which has nothing
    to do with the limiter — timing the fetch would be asserting the network is fast.
    """
    await scraper.fetch(BASE)
    start = time.monotonic()
    await scraper.rate_limiter.wait("https://quotes.toscrape.com/")
    limiter_delay = time.monotonic() - start  # (MEASURED, seconds) limiter only
    assert limiter_delay < 0.1, (
        f"the limiter made a fresh domain wait {limiter_delay:.2f}s; "
        "the delay is supposed to be per-domain"
    )
    print(f"\nlimiter contributed {limiter_delay:.3f}s for a fresh domain")


async def test_js_render_path(scraper):
    result = await scraper.fetch(BASE, js_render=True)
    assert result.status_code == 200
    assert len(result.markdown) > 500
    print(f"\njs-rendered {len(result.markdown)}b of markdown")
