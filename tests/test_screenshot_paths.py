"""Where screenshots land. The path is part of the spec, not an afterthought."""

from __future__ import annotations

from pathlib import Path

from cute_web_scraper.server import _screenshot_path


def test_default_path_is_durable_not_a_temp_dir():
    """A screenshot is a file someone opens later; /tmp is cleared daily."""
    path = _screenshot_path("https://example.com/page", "")
    assert path.is_relative_to(Path.home())
    assert "tmp" not in str(path).lower().split("/")[:3]
    assert path.suffix == ".png"


def test_default_path_is_named_after_the_host():
    assert "example.com" in _screenshot_path("https://example.com/deep/page", "").name


def test_a_port_does_not_break_the_filename():
    name = _screenshot_path("http://localhost:8080/x", "").name
    assert ":" not in name and "localhost_8080" in name


def test_explicit_path_is_honoured():
    assert _screenshot_path("https://e.com", "/tmp/shot.png") == Path("/tmp/shot.png")


def test_a_missing_extension_is_corrected():
    assert _screenshot_path("https://e.com", "/tmp/shot").suffix == ".png"


def test_a_wrong_extension_is_corrected():
    assert _screenshot_path("https://e.com", "/tmp/shot.jpg").name == "shot.png"


def test_home_is_expanded():
    assert "~" not in str(_screenshot_path("https://e.com", "~/shots/a.png"))


def test_a_url_with_no_host_still_produces_a_file():
    assert _screenshot_path("not-a-url", "").suffix == ".png"
