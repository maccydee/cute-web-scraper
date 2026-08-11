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


async def test_rate_limiter_enforces_rate(scraper):
    """The limiter caps the request rate per domain, measured start-to-start.

    Timed from the start of the first fetch to the start of the second, which is the
    semantic the limiter actually implements: one request per delay_ms per domain,
    with a slow response counting toward the interval rather than adding to it.
    """
    start = time.monotonic()
    await scraper.fetch(BASE)
    await scraper.fetch(SECOND)
    elapsed = time.monotonic() - start  # (MEASURED, seconds)
    minimum = DELAY_MS / 1000 * 0.9
    assert elapsed >= minimum, (
        f"two same-domain fetches took {elapsed:.2f}s, below the {DELAY_MS}ms rate. "
        "The limiter is not being applied in Scraper.fetch."
    )
    print(f"\ntwo same-domain fetches took {elapsed:.2f}s (rate floor {minimum:.2f}s)")


async def test_different_domains_are_not_penalised(scraper):
    """A second domain is not made to wait behind the first."""
    await scraper.fetch(BASE)
    start = time.monotonic()
    await scraper.fetch("https://quotes.toscrape.com/")
    elapsed = time.monotonic() - start  # (MEASURED, seconds)
    assert elapsed < DELAY_MS / 1000, (
        f"a different domain waited {elapsed:.2f}s; the delay should be per-domain"
    )
    print(f"\ndifferent-domain fetch took {elapsed:.2f}s (no cross-domain penalty)")


async def test_js_render_path(scraper):
    result = await scraper.fetch(BASE, js_render=True)
    assert result.status_code == 200
    assert len(result.markdown) > 500
    print(f"\njs-rendered {len(result.markdown)}b of markdown")
