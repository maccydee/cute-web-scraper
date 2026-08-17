from unittest.mock import patch

import httpx
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
        # Off by default here: curl_cffi uses its own HTTP stack, which pytest_httpx
        # cannot intercept, so leaving it on would let real requests escape the
        # unit suite. Escalation has its own tests below with the tier mocked.
        impersonate=False,
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


# ------------------------------------------------- TLS-fingerprint escalation


async def test_no_escalation_when_the_plain_request_succeeds(httpx_mock):
    """The fast path must stay untouched for the sites that never block."""
    httpx_mock.add_response(html="<h1>Fine</h1>")
    calls: list[str] = []

    async def spy(self, url, profile):
        calls.append(profile)
        raise AssertionError("should not escalate")

    async with Scraper(_config(impersonate=True)) as s:
        with patch.object(Scraper, "_fetch_impersonated", new=spy):
            result = await s.fetch("https://example.com")
    assert result.via == "httpx"
    assert calls == []


async def test_block_escalates_and_first_profile_clears_it(httpx_mock):
    httpx_mock.add_response(status_code=403)
    tried: list[str] = []

    async def spy(self, url, profile):
        tried.append(profile)
        return "<h1>Products</h1>", 200, "text/html"

    async with Scraper(_config(impersonate=True)) as s:
        with patch.object(Scraper, "_fetch_impersonated", new=spy):
            result = await s.fetch("https://blocked.example")
    assert result.blocked is False
    assert result.via == "impersonate:chrome"
    assert "# Products" in result.markdown
    assert tried == ["chrome"]


async def test_falls_through_to_the_second_profile(httpx_mock):
    """eBay rejects the Chrome fingerprint but accepts Safari's."""
    httpx_mock.add_response(status_code=403)
    tried: list[str] = []

    async def spy(self, url, profile):
        tried.append(profile)
        if profile == "chrome":
            return "", 403, "text/html"
        return "<h1>Listings</h1>", 200, "text/html"

    async with Scraper(_config(impersonate=True)) as s:
        with patch.object(Scraper, "_fetch_impersonated", new=spy):
            result = await s.fetch("https://ebay.example")
    assert result.blocked is False
    assert result.via == "impersonate:safari"
    assert tried == ["chrome", "safari"]


async def test_still_blocked_after_every_profile(httpx_mock):
    httpx_mock.add_response(status_code=403)

    async def spy(self, url, profile):
        return "", 403, "text/html"

    async with Scraper(_config(impersonate=True)) as s:
        with patch.object(Scraper, "_fetch_impersonated", new=spy):
            result = await s.fetch("https://hopeless.example")
    assert result.blocked is True
    assert result.block_reason == "http_403"
    assert result.via == "impersonate:exhausted"


async def test_a_failing_profile_does_not_abort_the_escalation(httpx_mock):
    httpx_mock.add_response(status_code=403)

    async def spy(self, url, profile):
        if profile == "chrome":
            raise RuntimeError("curl exploded")
        return "<h1>Recovered</h1>", 200, "text/html"

    async with Scraper(_config(impersonate=True)) as s:
        with patch.object(Scraper, "_fetch_impersonated", new=spy):
            result = await s.fetch("https://flaky.example")
    assert result.via == "impersonate:safari"


async def test_escalation_can_be_switched_off(httpx_mock):
    httpx_mock.add_response(status_code=403)

    async def spy(self, url, profile):
        raise AssertionError("must not escalate when disabled")

    async with Scraper(_config(impersonate=False)) as s:
        with patch.object(Scraper, "_fetch_impersonated", new=spy):
            result = await s.fetch("https://blocked.example")
    assert result.blocked is True
    assert result.via == "httpx"


async def test_cleared_block_does_not_penalise_the_domain(httpx_mock):
    """If escalation worked, the domain isn't hostile — don't widen its delay."""
    httpx_mock.add_response(status_code=403)

    async def spy(self, url, profile):
        return "<h1>ok</h1>", 200, "text/html"

    async with Scraper(_config(impersonate=True, delay_ms=100)) as s:
        before = s.rate_limiter.current_delay("https://blocked.example")
        with patch.object(Scraper, "_fetch_impersonated", new=spy):
            await s.fetch("https://blocked.example")
        after = s.rate_limiter.current_delay("https://blocked.example")
    assert after <= before


async def test_transport_error_also_escalates(httpx_mock):
    """ASOS drops the connection rather than answering 403 — that must escalate too."""
    httpx_mock.add_exception(httpx.ReadError(""))

    async def spy(self, url, profile):
        return "<h1>Products</h1>", 200, "text/html"

    async with Scraper(_config(impersonate=True)) as s:
        with patch.object(Scraper, "_fetch_impersonated", new=spy):
            result = await s.fetch("https://asos.example")
    assert result.blocked is False
    assert result.via == "impersonate:chrome"
    assert "# Products" in result.markdown


async def test_transport_error_with_no_rescue_raises(httpx_mock):
    """An empty body with status 0 would read as a blank page; raise instead."""
    httpx_mock.add_exception(httpx.ReadError("boom"))

    async def spy(self, url, profile):
        raise RuntimeError("also failed")

    async with Scraper(_config(impersonate=True)) as s:
        with patch.object(Scraper, "_fetch_impersonated", new=spy):
            with pytest.raises(httpx.ReadError):
                await s.fetch("https://asos.example")


async def test_transport_error_propagates_when_escalation_is_off(httpx_mock):
    httpx_mock.add_exception(httpx.ReadError("boom"))
    async with Scraper(_config(impersonate=False)) as s:
        with pytest.raises(httpx.ReadError):
            await s.fetch("https://asos.example")


async def test_impersonated_fetch_follows_redirects():
    """curl_cffi does not follow redirects by default; without this eBay came back
    as a 13KB stub instead of the 2.1MB results page."""
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200
        text = "<h1>ok</h1>"
        headers = {"content-type": "text/html"}

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kwargs):
            captured.update(kwargs)
            return FakeResponse()

    import curl_cffi.requests

    async with Scraper(_config()) as s:
        with patch.object(curl_cffi.requests, "AsyncSession", FakeSession):
            await s._fetch_impersonated("https://example.com", "chrome")

    assert captured.get("allow_redirects") is True
    assert captured.get("impersonate") == "chrome"


@pytest.mark.parametrize(
    "body",
    [
        "<title>Pardon our interruption...</title>",
        "<p>Checking your browser before you access eBay.</p>",
    ],
)
async def test_interstitial_served_with_200_is_a_block(httpx_mock, body):
    """eBay returns its Akamai interstitial with HTTP 200. Status alone misses it,
    and accepting it would return a 13KB stub as though it were the page."""
    httpx_mock.add_response(status_code=200, html=body)
    async with Scraper(_config()) as s:
        result = await s.fetch("https://ebay.example")
    assert result.blocked is True
    assert result.block_reason == "challenge"


async def test_interstitial_keeps_the_escalation_going(httpx_mock):
    """A 200 interstitial from one profile must not end the search early."""
    httpx_mock.add_response(status_code=403)
    tried: list[str] = []

    async def spy(self, url, profile):
        tried.append(profile)
        if profile == "chrome":
            return "<title>Pardon our interruption...</title>", 200, "text/html"
        return "<h1>Listings</h1>", 200, "text/html"

    async with Scraper(_config(impersonate=True)) as s:
        with patch.object(Scraper, "_fetch_impersonated", new=spy):
            result = await s.fetch("https://ebay.example")
    assert tried == ["chrome", "safari"]
    assert result.via == "impersonate:safari"
    assert result.blocked is False


async def test_cloudflare_script_on_a_real_page_is_not_a_block(httpx_mock):
    """Cloudflare injects /cdn-cgi/challenge-platform into pages it successfully
    serves. Found live: a real 716KB Waterstones page was flagged as blocked."""
    body = (
        "<html><head><title>Atomic Habits | Waterstones</title>"
        '<script src="/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1"></script>'
        "</head><body>"
        + "<p>"
        + ("Real book description copy. " * 400)
        + "</p>"
        + "".join(f'<a href="/book/{i}">Book {i}</a>' for i in range(40))
        + "</body></html>"
    )
    httpx_mock.add_response(status_code=200, html=body)
    async with Scraper(_config()) as s:
        result = await s.fetch("https://waterstones.example")
    assert result.blocked is False
    assert "Real book description copy" in result.markdown


async def test_challenge_page_without_content_is_still_a_block(httpx_mock):
    httpx_mock.add_response(
        status_code=403,
        html='<html><body><script src="/cdn-cgi/challenge-platform/x"></script>'
        "<p>Checking your browser</p></body></html>",
    )
    async with Scraper(_config()) as s:
        result = await s.fetch("https://blocked.example")
    assert result.blocked is True
    assert result.block_reason == "challenge"


async def test_google_consent_wall_is_a_block(httpx_mock):
    """Served with HTTP 200 and no challenge markers, so nothing else caught it."""
    httpx_mock.add_response(
        status_code=200,
        html="<html><head><title>Before you continue to Google Maps</title></head>"
        "<body><p>Before you continue to Google Maps</p></body></html>",
    )
    async with Scraper(_config()) as s:
        result = await s.fetch("https://www.google.com/maps/place/x")
    assert result.blocked is True
    assert result.block_reason == "challenge"


async def test_bot_check_under_an_unusual_status_is_a_block(httpx_mock):
    """Booking.com serves its bot check with HTTP 202, which is not a block status
    at all, so only the body reveals it."""
    httpx_mock.add_response(
        status_code=202,
        html="<html><body><p>JavaScript is disabled. In order to continue, we need to "
        "verify that you're not a robot.</p></body></html>",
    )
    async with Scraper(_config()) as s:
        result = await s.fetch("https://booking.example")
    assert result.blocked is True
    assert result.block_reason == "challenge"


# --------------------------------------------------------------- PDF handling

_PDF = (__import__("pathlib").Path(__file__).parent / "fixtures" / "sample.pdf").read_bytes()


async def test_pdf_is_extracted_not_blanked(httpx_mock):
    """A PDF link used to return front matter and nothing at all."""
    httpx_mock.add_response(
        status_code=200, headers={"Content-Type": "application/pdf"}, content=_PDF
    )
    async with Scraper(_config()) as s:
        result = await s.fetch("https://example.com/report.pdf")
    assert result.blocked is False
    assert "Quarterly revenue was 4.2 million" in result.markdown
    assert result.content_type.startswith("application/pdf")


async def test_a_corrupt_pdf_does_not_crash(httpx_mock):
    httpx_mock.add_response(
        status_code=200, headers={"Content-Type": "application/pdf"}, content=b"not a pdf at all"
    )
    async with Scraper(_config()) as s:
        result = await s.fetch("https://example.com/broken.pdf")
    assert result.markdown == ""
    assert result.blocked is False


async def test_binary_types_other_than_pdf_are_still_skipped(httpx_mock):
    httpx_mock.add_response(
        status_code=200, headers={"Content-Type": "image/png"}, content=b"\x89PNG\r\n\x1a\n"
    )
    async with Scraper(_config()) as s:
        result = await s.fetch("https://example.com/logo.png")
    assert result.markdown == ""


# ----------------------------------------------------------- main content mode


async def test_article_pages_isolate_the_main_body_by_default(httpx_mock):
    body = "The genuine article body a reader came for. " * 40
    page = (
        "<html><head><title>News</title></head><body>"
        "<nav><a href='/a'>Home</a><a href='/b'>Sport</a></nav>"
        "<div>We use cookies on this site. Accept all cookies?</div>"
        f"<article><h1>Headline</h1><p>{body}</p></article>"
        "<footer>Copyright 2026 Example Media Group</footer>"
        "</body></html>"
    )
    httpx_mock.add_response(html=page)
    async with Scraper(_config()) as s:
        result = await s.fetch("https://news.example/story")
    assert "genuine article body" in result.markdown
    assert "cookies" not in result.markdown.lower()
    assert "Copyright 2026" not in result.markdown


async def test_listing_pages_keep_the_whole_document(httpx_mock):
    listing = (
        "<html><body><div class='grid'>"
        + "".join(f"<a href='/p/{i}'>Product {i}</a>" for i in range(40))
        + "</div></body></html>"
    )
    httpx_mock.add_response(html=listing)
    async with Scraper(_config()) as s:
        result = await s.fetch("https://shop.example/all")
    assert "Product 7" in result.markdown


async def test_main_content_can_be_forced_off(httpx_mock):
    body = "The genuine article body a reader came for. " * 40
    page = (
        f"<html><body><article><p>{body}</p></article><footer>Copyright 2026</footer></body></html>"
    )
    httpx_mock.add_response(html=page)
    async with Scraper(_config()) as s:
        result = await s.fetch("https://news.example/story", main_content=False)
    assert "Copyright 2026" in result.markdown
