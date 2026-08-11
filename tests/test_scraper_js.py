import asyncio
from unittest.mock import AsyncMock, patch

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


async def test_rendered_fetch_returns_markdown():
    async with Scraper(_config()) as s:
        with patch.object(
            Scraper,
            "_fetch_rendered",
            new=AsyncMock(return_value=("<h1>JS Rendered</h1>", 200, "text/html")),
        ):
            result = await s.fetch("https://example.com", js_render=True)
    assert "# JS Rendered" in result.markdown
    assert result.status_code == 200


async def test_browser_launches_only_once_under_concurrency():
    """Regression: without the single-flight lock, N parallel fetches launch N browsers."""
    fake_page = AsyncMock()
    fake_page.content = AsyncMock(return_value="<h1>ok</h1>")
    fake_page.goto = AsyncMock(return_value=None)
    fake_browser = AsyncMock()
    fake_browser.new_page = AsyncMock(return_value=fake_page)

    launch_calls = 0

    async def fake_launch(self, playwright):
        nonlocal launch_calls
        launch_calls += 1
        await asyncio.sleep(0.01)  # widen the race window
        return fake_browser

    async with Scraper(_config()) as s:
        with (
            patch("cute_web_scraper.scraper._start_playwright", new=AsyncMock()),
            patch.object(Scraper, "_launch", new=fake_launch),
        ):
            await asyncio.gather(
                *(s.fetch(f"https://example.com/{i}", js_render=True) for i in range(5))
            )

    assert launch_calls == 1, f"expected a single browser launch, got {launch_calls}"


async def test_install_is_attempted_once_when_launch_fails():
    """A failed launch triggers exactly one install attempt, then a relaunch."""
    fake_page = AsyncMock()
    fake_page.content = AsyncMock(return_value="<h1>ok</h1>")
    fake_page.goto = AsyncMock(return_value=None)
    fake_browser = AsyncMock()
    fake_browser.new_page = AsyncMock(return_value=fake_page)

    attempts = 0

    async def flaky_launch(self, playwright):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("chromium missing")
        return fake_browser

    install = AsyncMock()
    async with Scraper(_config()) as s:
        with (
            patch("cute_web_scraper.scraper._start_playwright", new=AsyncMock()),
            patch("cute_web_scraper.scraper._install_chromium", new=install),
            patch.object(Scraper, "_launch", new=flaky_launch),
        ):
            await s.fetch("https://example.com", js_render=True)

    assert install.await_count == 1
    assert attempts == 2


async def test_driver_stopped_when_relaunch_fails():
    """A permanently broken launch must not leak the playwright driver.

    Impersonation is off here so the failure surfaces directly; with it on, a
    browser that cannot start falls back to the static tier instead.
    """
    driver = AsyncMock()

    async def always_fails(self, playwright):
        raise RuntimeError("chromium broken")

    async with Scraper(_config(impersonate=False)) as s:
        with (
            patch("cute_web_scraper.scraper._start_playwright", new=AsyncMock(return_value=driver)),
            patch("cute_web_scraper.scraper._install_chromium", new=AsyncMock()),
            patch.object(Scraper, "_launch", new=always_fails),
        ):
            with pytest.raises(RuntimeError, match="chromium broken"):
                await s.fetch("https://example.com", js_render=True)

    driver.stop.assert_awaited_once()


async def test_unlaunchable_browser_falls_back_instead_of_failing(httpx_mock):
    """If Chromium cannot start at all, a static answer beats no answer."""
    httpx_mock.add_response(html="<h1>Static</h1>")
    driver = AsyncMock()

    async def always_fails(self, playwright):
        raise RuntimeError("chromium broken")

    async with Scraper(_config(impersonate=True)) as s:
        with (
            patch("cute_web_scraper.scraper._start_playwright", new=AsyncMock(return_value=driver)),
            patch("cute_web_scraper.scraper._install_chromium", new=AsyncMock()),
            patch.object(Scraper, "_launch", new=always_fails),
        ):
            result = await s.fetch("https://example.com", js_render=True)
    assert result.via == "httpx"
    assert "# Static" in result.markdown


async def test_invalid_scheme_before_browser_work():
    async with Scraper(_config()) as s:
        with pytest.raises(ValueError, match="http"):
            await s.fetch("ftp://example.com", js_render=True)


async def test_blocked_browser_falls_back_to_impersonation(httpx_mock):
    """ASOS 403s Playwright but lets curl_cffi through; js_render must not lose that."""
    httpx_mock.add_response(status_code=403)

    async def rendered(self, url):
        return "", 403, "text/html"

    async def impersonated(self, url, profile):
        return "<h1>Products</h1>", 200, "text/html"

    async with Scraper(_config(impersonate=True)) as s:
        with (
            patch.object(Scraper, "_fetch_rendered", new=rendered),
            patch.object(Scraper, "_fetch_impersonated", new=impersonated),
        ):
            result = await s.fetch("https://asos.example", js_render=True)
    assert result.blocked is False
    assert result.via == "impersonate:chrome131"
    assert "# Products" in result.markdown


async def test_successful_render_does_not_fall_back(httpx_mock):
    async def rendered(self, url):
        return "<h1>Rendered</h1>", 200, "text/html"

    async def impersonated(self, url, profile):
        raise AssertionError("must not fall back when rendering worked")

    async with Scraper(_config(impersonate=True)) as s:
        with (
            patch.object(Scraper, "_fetch_rendered", new=rendered),
            patch.object(Scraper, "_fetch_impersonated", new=impersonated),
        ):
            result = await s.fetch("https://example.com", js_render=True)
    assert result.via == "playwright"


async def test_both_routes_blocked_keeps_the_browser_answer(httpx_mock):
    httpx_mock.add_response(status_code=403)

    async def rendered(self, url):
        return "<p>browser block</p>", 403, "text/html"

    async def impersonated(self, url, profile):
        return "", 403, "text/html"

    async with Scraper(_config(impersonate=True)) as s:
        with (
            patch.object(Scraper, "_fetch_rendered", new=rendered),
            patch.object(Scraper, "_fetch_impersonated", new=impersonated),
        ):
            result = await s.fetch("https://trustpilot.example", js_render=True)
    assert result.blocked is True
    assert result.via == "playwright"


# ------------------------------------------------------ stealth: the last resort


async def test_403_with_a_full_page_is_not_a_block(httpx_mock):
    """Trustpilot answers 403 while serving the whole reviews page. Trusting the
    status alone would blank a megabyte of real content."""
    body = (
        "<html><body>"
        + "<p>"
        + ("Genuine review copy for a real product. " * 300)
        + "</p>"
        + "".join(f'<a href="/reviews/{i}">Review {i}</a>' for i in range(30))
        + "</body></html>"
    )
    httpx_mock.add_response(status_code=403, html=body)
    async with Scraper(_config(impersonate=False)) as s:
        result = await s.fetch("https://trustpilot.example")
    assert result.blocked is False
    assert "Genuine review copy" in result.markdown


async def test_403_with_no_content_is_still_a_block(httpx_mock):
    httpx_mock.add_response(status_code=403, html="<html><body>Access Denied</body></html>")
    async with Scraper(_config(impersonate=False)) as s:
        result = await s.fetch("https://blocked.example")
    assert result.blocked is True
    assert result.block_reason == "http_403"


async def test_stealth_runs_only_after_everything_else_failed(httpx_mock):
    httpx_mock.add_response(status_code=403)
    order: list[str] = []

    async def impersonated(self, url, profile):
        order.append(f"impersonate:{profile}")
        return "", 403, "text/html"

    async def stealth(self, url):
        order.append("stealth")
        return "<h1>Reviews</h1>", 200, "text/html"

    async with Scraper(_config(impersonate=True)) as s:
        with (
            patch.object(Scraper, "_fetch_impersonated", new=impersonated),
            patch.object(Scraper, "_fetch_stealth", new=stealth),
        ):
            result = await s.fetch("https://trustpilot.example")
    assert order == ["impersonate:chrome131", "impersonate:safari17_0", "stealth"]
    assert result.via == "stealth"
    assert result.blocked is False


async def test_stealth_never_runs_when_an_earlier_tier_worked(httpx_mock):
    httpx_mock.add_response(html="<h1>Fine</h1>")

    async def stealth(self, url):
        raise AssertionError("stealth must be a last resort")

    async with Scraper(_config(impersonate=True)) as s:
        with patch.object(Scraper, "_fetch_stealth", new=stealth):
            result = await s.fetch("https://example.com")
    assert result.via == "httpx"


async def test_stealth_can_be_disabled(httpx_mock):
    httpx_mock.add_response(status_code=403)

    async def impersonated(self, url, profile):
        return "", 403, "text/html"

    async def stealth(self, url):
        raise AssertionError("must not run when disabled")

    async with Scraper(_config(impersonate=True, stealth=False)) as s:
        with (
            patch.object(Scraper, "_fetch_impersonated", new=impersonated),
            patch.object(Scraper, "_fetch_stealth", new=stealth),
        ):
            result = await s.fetch("https://blocked.example")
    assert result.blocked is True
    assert result.via == "impersonate:exhausted"


async def test_a_failing_stealth_tier_is_not_fatal(httpx_mock):
    httpx_mock.add_response(status_code=403)

    async def impersonated(self, url, profile):
        return "", 403, "text/html"

    async def stealth(self, url):
        raise RuntimeError("stealth browser exploded")

    async with Scraper(_config(impersonate=True)) as s:
        with (
            patch.object(Scraper, "_fetch_impersonated", new=impersonated),
            patch.object(Scraper, "_fetch_stealth", new=stealth),
        ):
            result = await s.fetch("https://blocked.example")
    assert result.blocked is True  # reported honestly, not crashed
