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

    async def rendered(self, url, **kwargs):
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
    assert result.via == "impersonate:chrome"
    assert "# Products" in result.markdown


async def test_successful_render_does_not_fall_back(httpx_mock):
    async def rendered(self, url, **kwargs):
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

    async def rendered(self, url, **kwargs):
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

    async def stealth(self, url, **kwargs):
        order.append("stealth")
        return "<h1>Reviews</h1>", 200, "text/html"

    async with Scraper(_config(impersonate=True)) as s:
        with (
            patch.object(Scraper, "_fetch_impersonated", new=impersonated),
            patch.object(Scraper, "_fetch_stealth", new=stealth),
        ):
            result = await s.fetch("https://trustpilot.example")
    assert order == ["impersonate:chrome", "impersonate:safari", "stealth"]
    assert result.via == "stealth"
    assert result.blocked is False


async def test_stealth_never_runs_when_an_earlier_tier_worked(httpx_mock):
    httpx_mock.add_response(html="<h1>Fine</h1>")

    async def stealth(self, url, **kwargs):
        raise AssertionError("stealth must be a last resort")

    async with Scraper(_config(impersonate=True)) as s:
        with patch.object(Scraper, "_fetch_stealth", new=stealth):
            result = await s.fetch("https://example.com")
    assert result.via == "httpx"


async def test_stealth_can_be_disabled(httpx_mock):
    httpx_mock.add_response(status_code=403)

    async def impersonated(self, url, profile):
        return "", 403, "text/html"

    async def stealth(self, url, **kwargs):
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

    async def stealth(self, url, **kwargs):
        raise RuntimeError("stealth browser exploded")

    async with Scraper(_config(impersonate=True)) as s:
        with (
            patch.object(Scraper, "_fetch_impersonated", new=impersonated),
            patch.object(Scraper, "_fetch_stealth", new=stealth),
        ):
            result = await s.fetch("https://blocked.example")
    assert result.blocked is True  # reported honestly, not crashed


# ------------------------------------------------------------ render settling


async def test_render_settles_before_reading_content():
    """Found by review: goto() returns on `load`, before an SPA has painted, and
    content() was read immediately. The stealth tier waited 4s; this one waited 0."""
    waits: list[int] = []
    page = AsyncMock()
    page.content = AsyncMock(return_value="<h1>ok</h1>")
    page.goto = AsyncMock(return_value=None)
    page.wait_for_timeout = AsyncMock(side_effect=lambda ms: waits.append(ms))
    browser = AsyncMock()
    browser.new_page = AsyncMock(return_value=page)

    async with Scraper(_config()) as s:
        with patch.object(Scraper, "_ensure_browser", new=AsyncMock(return_value=browser)):
            await s.fetch("https://spa.example", js_render=True)
    assert waits and waits[0] > 0, "a rendered page must be given time to populate"


async def test_wait_for_selector_is_preferred_over_a_fixed_delay():
    page = AsyncMock()
    page.content = AsyncMock(return_value="<h1>ok</h1>")
    page.goto = AsyncMock(return_value=None)
    page.wait_for_selector = AsyncMock(return_value=None)
    page.wait_for_timeout = AsyncMock()
    browser = AsyncMock()
    browser.new_page = AsyncMock(return_value=page)

    async with Scraper(_config()) as s:
        with patch.object(Scraper, "_ensure_browser", new=AsyncMock(return_value=browser)):
            await s.fetch("https://spa.example", js_render=True, wait_for=".results")
    page.wait_for_selector.assert_awaited_once()
    page.wait_for_timeout.assert_not_awaited()


async def test_a_missing_selector_falls_back_to_the_delay():
    page = AsyncMock()
    page.content = AsyncMock(return_value="<h1>ok</h1>")
    page.goto = AsyncMock(return_value=None)
    page.wait_for_selector = AsyncMock(side_effect=RuntimeError("timeout"))
    page.wait_for_timeout = AsyncMock()
    browser = AsyncMock()
    browser.new_page = AsyncMock(return_value=page)

    async with Scraper(_config()) as s:
        with patch.object(Scraper, "_ensure_browser", new=AsyncMock(return_value=browser)):
            result = await s.fetch("https://spa.example", js_render=True, wait_for=".nope")
    page.wait_for_timeout.assert_awaited()
    assert result.blocked is False


async def test_a_tuned_wait_bypasses_the_cache():
    """A caller asking for a longer wait wants a fresh render, not the cached one."""
    page = AsyncMock()
    page.content = AsyncMock(return_value="<h1>ok</h1>")
    page.goto = AsyncMock(return_value=None)
    browser = AsyncMock()
    browser.new_page = AsyncMock(return_value=page)

    async with Scraper(_config(cache_ttl_s=300)) as s:
        with patch.object(Scraper, "_ensure_browser", new=AsyncMock(return_value=browser)):
            first = await s.fetch("https://spa.example", js_render=True)
            again = await s.fetch("https://spa.example", js_render=True)
            tuned = await s.fetch("https://spa.example", js_render=True, wait_ms=3000)
    assert first.from_cache is False
    assert again.from_cache is True
    assert tuned.from_cache is False


async def test_a_disconnected_browser_is_relaunched():
    """A crashed Chromium stayed non-None forever, failing every later js_render."""
    dead = AsyncMock()
    dead.is_connected = lambda: False
    live_page = AsyncMock()
    live_page.content = AsyncMock(return_value="<h1>back</h1>")
    live_page.goto = AsyncMock(return_value=None)
    live = AsyncMock()
    live.is_connected = lambda: True
    live.new_page = AsyncMock(return_value=live_page)

    async def relaunch(self, playwright):
        return live

    async with Scraper(_config()) as s:
        s._browser = dead
        with (
            patch("cute_web_scraper.scraper._start_playwright", new=AsyncMock()),
            patch.object(Scraper, "_launch", new=relaunch),
        ):
            result = await s.fetch("https://example.com", js_render=True)
    assert "# back" in result.markdown


# ------------------------------------------------- network capture & actions


def _fake_response(url, ctype="application/json", body='{"items":[1,2,3]}', status=200):
    r = AsyncMock()
    r.url = url
    r.status = status
    r.headers = {"content-type": ctype}
    r.request = type("Req", (), {"method": "GET"})()
    r.text = AsyncMock(return_value=body)
    return r


def _page_with_responses(responses):
    page = AsyncMock()
    page.content = AsyncMock(return_value="<h1>ok</h1>")
    page.goto = AsyncMock(return_value=None)
    handlers = {}
    page.on = lambda event, fn: handlers.setdefault(event, fn)
    page._handlers = handlers
    page._responses = responses

    async def goto(*a, **k):
        for resp in responses:
            await handlers["response"](resp)
        return None

    page.goto = AsyncMock(side_effect=goto)
    return page


async def test_network_capture_keeps_json_and_drops_the_rest():
    """The JSON behind a JS page is the data; the CSS and images are not."""
    page = _page_with_responses(
        [
            _fake_response("https://api.example/products", body='{"products":[{"id":1}]}'),
            _fake_response("https://cdn.example/app.css", ctype="text/css", body="body{}"),
            _fake_response("https://cdn.example/logo.png", ctype="image/png", body=""),
        ]
    )
    browser = AsyncMock()
    browser.new_page = AsyncMock(return_value=page)

    async with Scraper(_config()) as s:
        with patch.object(Scraper, "_ensure_browser", new=AsyncMock(return_value=browser)):
            captured = await s.capture_network("https://shop.example")

    assert len(captured) == 1
    assert captured[0]["url"] == "https://api.example/products"
    assert '"products"' in captured[0]["body"]
    assert captured[0]["status"] == 200


async def test_network_capture_can_widen_beyond_json():
    page = _page_with_responses(
        [_fake_response("https://cdn.example/app.css", ctype="text/css", body="body{}")]
    )
    browser = AsyncMock()
    browser.new_page = AsyncMock(return_value=page)
    async with Scraper(_config()) as s:
        with patch.object(Scraper, "_ensure_browser", new=AsyncMock(return_value=browser)):
            captured = await s.capture_network("https://x.example", include_types=("css",))
    assert len(captured) == 1


async def test_network_capture_truncates_large_bodies():
    page = _page_with_responses([_fake_response("https://api.example/big", body="x" * 50_000)])
    browser = AsyncMock()
    browser.new_page = AsyncMock(return_value=page)
    async with Scraper(_config()) as s:
        with patch.object(Scraper, "_ensure_browser", new=AsyncMock(return_value=browser)):
            captured = await s.capture_network("https://x.example", max_body_chars=100)
    assert len(captured[0]["body"]) == 100
    assert captured[0]["body_truncated"] is True
    assert captured[0]["size"] == 50_000


async def test_actions_run_in_order_and_report_what_happened():
    page = AsyncMock()
    page.click = AsyncMock()
    page.fill = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    async with Scraper(_config()) as s:
        log = await s.run_actions(
            page,
            [
                {"action": "click", "selector": "#accept"},
                {"action": "type", "selector": "#q", "text": "trainers"},
                {"action": "wait", "ms": 500},
            ],
        )
    assert "clicked #accept" in log[0]
    assert "typed into #q" in log[1]
    page.click.assert_awaited_once()
    page.fill.assert_awaited_once()


async def test_a_failing_action_does_not_abort_the_rest():
    page = AsyncMock()
    page.click = AsyncMock(side_effect=RuntimeError("no such element"))
    page.wait_for_timeout = AsyncMock()
    async with Scraper(_config()) as s:
        log = await s.run_actions(
            page, [{"action": "click", "selector": "#missing"}, {"action": "wait", "ms": 10}]
        )
    assert "failed" in log[0]
    assert "waited" in log[1]


async def test_unknown_actions_are_reported_not_silently_ignored():
    page = AsyncMock()
    async with Scraper(_config()) as s:
        log = await s.run_actions(page, [{"action": "teleport", "selector": "x"}])
    assert "unknown action" in log[0]


async def test_scroll_to_bottom_stops_when_the_page_stops_growing():
    page = AsyncMock()
    page.mouse.wheel = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    heights = iter([1000, 2000, 2000])
    page.evaluate = AsyncMock(side_effect=lambda _: next(heights))
    async with Scraper(_config()) as s:
        log = await s.run_actions(page, [{"action": "scroll_to_bottom", "max_rounds": 10}])
    assert "scrolled to bottom after 3 rounds" in log[0]


async def test_click_until_gone_stops_when_the_button_disappears():
    page = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    calls = {"n": 0}

    async def click(selector, timeout=None):
        calls["n"] += 1
        if calls["n"] > 3:
            raise RuntimeError("gone")

    page.click = AsyncMock(side_effect=click)
    async with Scraper(_config()) as s:
        log = await s.run_actions(page, [{"action": "click_until_gone", "selector": ".more"}])
    assert "3x until it disappeared" in log[0]


async def test_actions_bypass_the_cache():
    """A scripted interaction wants a fresh page, not whatever was cached."""
    page = AsyncMock()
    page.content = AsyncMock(return_value="<h1>ok</h1>")
    page.goto = AsyncMock(return_value=None)
    page.click = AsyncMock()
    browser = AsyncMock()
    browser.new_page = AsyncMock(return_value=page)
    async with Scraper(_config(cache_ttl_s=300)) as s:
        with patch.object(Scraper, "_ensure_browser", new=AsyncMock(return_value=browser)):
            await s.fetch("https://x.example", js_render=True)
            acted = await s.fetch(
                "https://x.example", js_render=True, actions=[{"action": "click", "selector": "#a"}]
            )
    assert acted.from_cache is False


async def test_the_headless_marker_is_stripped_from_the_user_agent():
    """Playwright advertises HeadlessChrome/151..., a plain automation flag: Reddit
    answers it with a 190KB shell and the same browser without it with 1MB."""
    page = AsyncMock()
    page.evaluate = AsyncMock(
        return_value=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) HeadlessChrome/151.0.7922.34 Safari/537.36"
        )
    )
    page.close = AsyncMock()
    browser = AsyncMock()
    browser.new_page = AsyncMock(return_value=page)

    async with Scraper(_config()) as s:
        ua = await s._browser_user_agent(browser)
    assert "HeadlessChrome" not in ua
    assert "Chrome/151.0.7922.34" in ua


async def test_the_user_agent_is_resolved_once_and_reused():
    page = AsyncMock()
    page.evaluate = AsyncMock(return_value="Mozilla/5.0 HeadlessChrome/1 Safari/537.36")
    page.close = AsyncMock()
    browser = AsyncMock()
    browser.new_page = AsyncMock(return_value=page)

    async with Scraper(_config()) as s:
        await s._browser_user_agent(browser)
        await s._browser_user_agent(browser)
    assert page.evaluate.await_count == 1


async def test_a_failure_to_read_the_user_agent_is_not_fatal():
    browser = AsyncMock()
    browser.new_page = AsyncMock(side_effect=RuntimeError("no page"))
    async with Scraper(_config()) as s:
        assert await s._browser_user_agent(browser) == ""
