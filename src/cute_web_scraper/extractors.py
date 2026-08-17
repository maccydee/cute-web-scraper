"""Structured data extraction from raw HTML.

Every function takes RAW HTML, not markdown. Markdown has no <a href> tags, so link
and social extraction cannot work from it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

_CONTEXT_CHARS = 60

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

_PHONE_RE = re.compile(
    r"""
    (?<![\w.])
    (?:
        \+\d{1,3}[\s.\-]?          # international prefix, or
        | \(?0\d{1,4}\)?[\s.\-]?   # national trunk prefix
    )
    (?:\d[\s.\-]?){6,12}\d
    (?![\w.])
    """,
    re.VERBOSE,
)
"""Requires a '+' country code or a leading-zero trunk code. A bare run of digits is
not a phone number -- that is what made a looser pattern match years and order IDs."""

_MIN_PHONE_DIGITS = 9
_MAX_PHONE_DIGITS = 15

_SOCIAL_DOMAINS: dict[str, str] = {
    "linkedin.com": "linkedin",
    "twitter.com": "twitter",
    "x.com": "twitter",
    "facebook.com": "facebook",
    "instagram.com": "instagram",
    "youtube.com": "youtube",
    "tiktok.com": "tiktok",
    "github.com": "github",
    "pinterest.com": "pinterest",
}


def extract_emails(html: str, url: str) -> list[dict[str, str]]:
    return _scan_text(html, url, _EMAIL_RE, _accept_any)


def extract_phones(html: str, url: str) -> list[dict[str, str]]:
    return _scan_text(html, url, _PHONE_RE, _is_plausible_phone)


def extract_links(html: str, url: str) -> list[dict[str, str]]:
    """All hyperlinks, resolved to absolute http(s) URLs."""
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for anchor in _anchors(html):
        href = str(anchor["href"]).strip()
        if not href or href.startswith("#"):
            continue
        absolute = urljoin(url, href)
        if urlparse(absolute).scheme not in ("http", "https"):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        results.append({"url": url, "value": absolute, "context": anchor.get_text(strip=True)})
    return results


def extract_social_links(html: str, url: str) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for anchor in _anchors(html):
        absolute = urljoin(url, str(anchor["href"]).strip())
        host = urlparse(absolute).netloc.lower().removeprefix("www.")
        platform = _SOCIAL_DOMAINS.get(host)
        if platform is None or absolute in seen:
            continue
        seen.add(absolute)
        results.append({"url": url, "platform": platform, "value": absolute})
    return results


def _anchors(html: str) -> list[Tag]:
    soup = BeautifulSoup(html, "lxml")
    return [a for a in soup.find_all("a", href=True) if isinstance(a, Tag)]


def _scan_text(
    html: str,
    url: str,
    pattern: re.Pattern[str],
    accept: Callable[[str], bool],
) -> list[dict[str, str]]:
    text = BeautifulSoup(html, "lxml").get_text(" ")
    seen: set[str] = set()
    results: list[dict[str, str]] = []
    for match in pattern.finditer(text):
        value = match.group().strip()
        if value in seen or not accept(value):
            continue
        seen.add(value)
        start = max(0, match.start() - _CONTEXT_CHARS)
        end = min(len(text), match.end() + _CONTEXT_CHARS)
        results.append({"url": url, "value": value, "context": text[start:end].strip()})
    return results


def _accept_any(_: str) -> bool:
    return True


def _is_plausible_phone(value: str) -> bool:
    digits = sum(c.isdigit() for c in value)
    return _MIN_PHONE_DIGITS <= digits <= _MAX_PHONE_DIGITS


def extract_by_selector(
    html: str, url: str, fields: dict[str, str], *, row_selector: str = ""
) -> list[dict[str, str]]:
    """Pull arbitrary fields out of a page with CSS selectors.

    The fixed extractors (email, phone, link, social, product) cover common shapes,
    but anything else — "the h1 and .price from these 200 pages" — had no path except
    dumping whole pages into the conversation.

    Without `row_selector` the page yields one row, each field taking that selector's
    first match. With it, each matching element becomes a row and the field selectors
    are resolved *within* that element, which is what turns a listing into a table.

    A selector suffixed with `@attr` reads that attribute instead of the text, so
    `"link": "a@href"` gives the URL rather than the anchor text.
    """
    if not html or not fields:
        return []
    soup = BeautifulSoup(html, "lxml")
    scopes = soup.select(row_selector) if row_selector else [soup]
    rows: list[dict[str, str]] = []
    for scope in scopes:
        row: dict[str, str] = {"url": url}
        for name, selector in fields.items():
            row[name] = _select_value(scope, selector, url) or ""
        if any(value for key, value in row.items() if key != "url"):
            rows.append(row)
    return rows


def _select_value(scope: Any, selector: str, url: str) -> str | None:
    expression, _, attribute = selector.partition("@")
    try:
        node = scope.select_one(expression.strip()) if expression.strip() else scope
    except Exception:  # noqa: BLE001 - an invalid selector yields nothing, not a crash
        return None
    if node is None:
        return None
    if attribute:
        raw = node.get(attribute.strip())
        if raw is None:
            return None
        value = " ".join(raw) if isinstance(raw, list) else str(raw)
        # Resolve href/src so the caller gets something fetchable.
        return urljoin(url, value) if attribute.strip() in ("href", "src") else value
    return node.get_text(" ", strip=True) or None
