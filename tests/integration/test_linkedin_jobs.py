"""Adversarial smoke test: LinkedIn job listings.

LinkedIn is the hard case: JavaScript-rendered, heavily anti-bot. It exercises the
Playwright tier and the adaptive backoff together.

    pytest -m integration tests/integration/test_linkedin_jobs.py -v -s

A SKIP here means LinkedIn blocked us and the backoff behaved correctly. A FAIL means
the scraper is wrong. Those are deliberately different outcomes.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from cute_web_scraper.config import Config
from cute_web_scraper.extractors import extract_links
from cute_web_scraper.scraper import FetchResult, Scraper

pytestmark = pytest.mark.integration

# The guest endpoint serves job cards without a login, unlike /jobs/search/.
JOBS_URL = (
    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    "?keywords=engineering%20manager&location=London&start=0"
)

_AUTH_WALL_MARKERS = (
    "/uas/login",
    "authwall",
    "sign in to linkedin",
    "join now to see",
)


def _config() -> Config:
    return Config(
        delay_ms=2000,  # deliberately polite against a hostile target
        max_concurrent=1,
        auth_token=None,
        user_data_dir=None,
        cache_ttl_s=0,
        cache_max_entries=10,
    )


@pytest_asyncio.fixture
async def scraper():
    async with Scraper(_config()) as s:
        yield s


def _classify(result: FetchResult) -> str:
    if result.blocked:
        return "blocked"
    if any(marker in result.html.lower() for marker in _AUTH_WALL_MARKERS):
        return "auth_wall"
    return "ok"


def _guard(result: FetchResult, scraper: Scraper) -> None:
    """Skip on a genuine block; fail loudly on an auth wall."""
    verdict = _classify(result)
    if verdict == "blocked":
        delay = scraper.rate_limiter.current_delay(JOBS_URL)
        pytest.skip(
            f"LinkedIn blocked the request ({result.block_reason}). "
            f"Backoff for linkedin.com is now {delay:.1f}s, which is correct behaviour. "
            "Retry later or set SCRAPER_CHROME_USER_DATA_DIR to reuse a logged-in profile."
        )
    if verdict == "auth_wall":
        pytest.fail(
            "LinkedIn served an authentication wall, so this endpoint no longer works "
            "anonymously. This is a scraper problem, not a LinkedIn outage: switch the "
            "URL or set SCRAPER_CHROME_USER_DATA_DIR. "
            f"First 300 chars:\n{result.html[:300]}"
        )


async def test_linkedin_jobs_render(scraper):
    """The Playwright tier renders LinkedIn's job cards."""
    result = await scraper.fetch(JOBS_URL, js_render=True)
    _guard(result, scraper)
    assert result.status_code == 200, f"unexpected status {result.status_code}"
    assert len(result.html) > 1000, "response too short to contain job cards"
    print(f"\nrendered {len(result.html)} bytes of HTML")


async def test_linkedin_job_links_extracted(scraper):
    """Structural assertion: real /jobs/view/ links, not keyword matching.

    This is the test v1 could never pass: it read links out of markdown, which has
    no <a href> tags.
    """
    result = await scraper.fetch(JOBS_URL, js_render=True)
    _guard(result, scraper)

    links = extract_links(result.html, result.url)
    job_links = [link for link in links if "/jobs/view/" in link["value"]]

    assert job_links, (
        f"No /jobs/view/ links found among {len(links)} total links. "
        "The page rendered and was not blocked, so LinkedIn's markup has likely "
        f"changed. First 500 chars:\n{result.html[:500]}"
    )
    print(f"\nfound {len(job_links)} job links:")
    for link in job_links[:3]:
        print(f"  {link['context'][:60]!r} -> {link['value'][:80]}")


async def test_backoff_engages_when_linkedin_pushes_back(scraper):
    """Whatever LinkedIn does, the limiter must end in a defensible state."""
    before = scraper.rate_limiter.current_delay(JOBS_URL)
    result = await scraper.fetch(JOBS_URL, js_render=True)
    after = scraper.rate_limiter.current_delay(JOBS_URL)

    if result.blocked:
        assert after > before, (
            f"Blocked but the delay did not increase ({before:.2f}s -> {after:.2f}s). "
            "Adaptive backoff is not wired up."
        )
        print(f"\nblocked; backoff {before:.2f}s -> {after:.2f}s (correct)")
    else:
        assert after <= max(before, _config().delay_ms / 1000), (
            "Request succeeded but the delay grew, which suggests a false-positive block detection."
        )
        print(f"\nsucceeded; delay steady at {after:.2f}s")
