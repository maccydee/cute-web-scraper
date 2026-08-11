import os
import subprocess
import sys


def _run(args: list[str], env_extra: dict[str, str] | None = None):
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run(
        [sys.executable, "-m", "cute_web_scraper", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_help_exits_zero():
    result = _run(["--help"])
    assert result.returncode == 0
    assert "--http" in result.stdout
    assert "--port" in result.stdout


def test_version_flag():
    result = _run(["--version"])
    assert result.returncode == 0
    assert "0.1.0" in result.stdout


def test_bad_config_fails_fast():
    result = _run([], {"SCRAPER_MAX_CONCURRENT": "0"})
    assert result.returncode != 0
    assert "SCRAPER_MAX_CONCURRENT" in result.stderr


def test_nothing_is_written_to_stdout_on_startup():
    """stdout is the JSON-RPC channel; log lines there corrupt the protocol."""
    result = _run([], {"SCRAPER_MAX_CONCURRENT": "0"})
    assert result.stdout == ""
