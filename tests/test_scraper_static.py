import pytest

from cute_web_scraper.config import Config
from cute_web_scraper.scraper import Scraper


def _config(**overrides) -> Config:
    base = dict(
        delay_ms=0,
        max_concurrent=5,
        auth_token=None,
        user_data_dir=None,
        cache_ttl_s=0,
        cache_max_entries=10,
    )
    base.update(overrides)
    return Config(**base)


async def test_fetch_success(httpx_mock):
    httpx_mock.add_response(
        html="<html><head><title>T</title></head><body><h1>Hi</h1></body></html>"
    )
    async with Scraper(_config()) as s:
        result = await s.fetch("https://example.com")
    assert "# Hi" in result.markdown
    assert "<h1>" in result.html
    assert result.title == "T"
    assert result.status_code == 200
    assert result.blocked is False
    assert result.block_reason is None


async def test_links_count(httpx_mock):
    httpx_mock.add_response(html='<a href="/a">A</a><a href="/b">B</a>')
    async with Scraper(_config()) as s:
        result = await s.fetch("https://example.com")
    assert result.links_count == 2


@pytest.mark.parametrize("status", [403, 429, 503])
async def test_block_statuses(httpx_mock, status):
    httpx_mock.add_response(status_code=status)
    async with Scraper(_config()) as s:
        result = await s.fetch("https://example.com")
    assert result.blocked is True
    assert result.block_reason == f"http_{status}"


async def test_challenge_body_marks_blocked(httpx_mock):
    httpx_mock.add_response(html='<div id="cf-browser-verification">checking</div>')
    async with Scraper(_config()) as s:
        result = await s.fetch("https://example.com")
    assert result.blocked is True
    assert result.block_reason == "challenge"


async def test_ordinary_prose_is_not_a_challenge(httpx_mock):
    """Regression: the bare phrase 'just a moment' must not blank a real page."""
    httpx_mock.add_response(html="<p>Just a moment, let me find that for you.</p>")
    async with Scraper(_config()) as s:
        result = await s.fetch("https://example.com")
    assert result.blocked is False
    assert "let me find that" in result.markdown


async def test_non_html_content_type_is_not_converted(httpx_mock):
    httpx_mock.add_response(
        status_code=200,
        headers={"Content-Type": "application/pdf"},
        content=b"%PDF-1.4 binary",
    )
    async with Scraper(_config()) as s:
        result = await s.fetch("https://example.com/doc.pdf")
    assert result.blocked is False
    assert result.markdown == ""
    assert result.content_type.startswith("application/pdf")


async def test_invalid_scheme():
    async with Scraper(_config()) as s:
        with pytest.raises(ValueError, match="http"):
            await s.fetch("ftp://example.com")


async def test_fetch_consults_the_rate_limiter(httpx_mock):
    """Proves the limiter is wired into fetch, without depending on wall clock.

    A live timing test cannot establish this: the limiter is start-to-start, so when a
    host is slower than the configured delay the network time absorbs the interval and
    the elapsed time is the same with or without the limiter.
    """
    httpx_mock.add_response(html="<h1>Hi</h1>")
    async with Scraper(_config()) as s:
        waited: list[str] = []
        original = s.rate_limiter.wait

        async def spy(url: str) -> None:
            waited.append(url)
            await original(url)

        s.rate_limiter.wait = spy  # type: ignore[method-assign]
        await s.fetch("https://example.com/page")

    assert waited == ["https://example.com/page"]


async def test_fetch_records_success_with_the_rate_limiter(httpx_mock):
    httpx_mock.add_response(status_code=429)
    async with Scraper(_config(delay_ms=100)) as s:
        before = s.rate_limiter.current_delay("https://example.com")
        await s.fetch("https://example.com")
        after = s.rate_limiter.current_delay("https://example.com")
    assert after > before, "a blocked fetch must widen the domain's delay"


async def test_cache_hit_skips_second_request(httpx_mock):
    httpx_mock.add_response(html="<h1>Once</h1>")
    async with Scraper(_config(cache_ttl_s=60)) as s:
        first = await s.fetch("https://example.com")
        second = await s.fetch("https://example.com")
    assert first.from_cache is False
    assert second.from_cache is True
    assert len(httpx_mock.get_requests()) == 1
