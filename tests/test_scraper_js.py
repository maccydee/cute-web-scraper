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
    """A permanently broken launch must not leak the playwright driver."""
    driver = AsyncMock()

    async def always_fails(self, playwright):
        raise RuntimeError("chromium broken")

    async with Scraper(_config()) as s:
        with (
            patch("cute_web_scraper.scraper._start_playwright", new=AsyncMock(return_value=driver)),
            patch("cute_web_scraper.scraper._install_chromium", new=AsyncMock()),
            patch.object(Scraper, "_launch", new=always_fails),
        ):
            with pytest.raises(RuntimeError, match="chromium broken"):
                await s.fetch("https://example.com", js_render=True)

    driver.stop.assert_awaited_once()


async def test_invalid_scheme_before_browser_work():
    async with Scraper(_config()) as s:
        with pytest.raises(ValueError, match="http"):
            await s.fetch("ftp://example.com", js_render=True)
