"""Web search, for when you have a question rather than a URL.

Uses DuckDuckGo's lite endpoint: no key, no quota, and a stable table layout that
parses cleanly. The request goes through the ordinary Scraper, so it inherits the
whole escalation chain — search engines block plain clients as readily as shops do.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlparse

from bs4 import BeautifulSoup, Tag

log = logging.getLogger(__name__)

SEARCH_URL = "https://lite.duckduckgo.com/lite/?q={query}"
_MAX_LIMIT = 50


class SearchError(Exception):
    """Raised when a search cannot be completed."""


def search_url(query: str) -> str:
    text = (query or "").strip()
    if not text:
        raise SearchError("Provide something to search for.")
    return SEARCH_URL.format(query=quote_plus(text))


def parse_results(html: str, limit: int = 10) -> list[dict[str, Any]]:
    """Pull ranked results out of a DuckDuckGo lite page."""
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    links = [a for a in soup.select("a.result-link") if isinstance(a, Tag)]
    snippets = [s.get_text(" ", strip=True) for s in soup.select(".result-snippet")]

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, anchor in enumerate(links):
        target = _real_url(str(anchor.get("href") or ""))
        if not target or target in seen:
            continue
        seen.add(target)
        results.append(
            {
                "rank": len(results) + 1,
                "title": anchor.get_text(" ", strip=True),
                "url": target,
                "snippet": snippets[index] if index < len(snippets) else "",
            }
        )
        if len(results) >= min(max(limit, 1), _MAX_LIMIT):
            break
    return results


def _real_url(href: str) -> str:
    """Unwrap DuckDuckGo's redirect so callers get a fetchable URL.

    Results link to //duckduckgo.com/l/?uddg=<encoded target>, which is useless to
    pass straight into fetch_pages.
    """
    if not href:
        return ""
    candidate = f"https:{href}" if href.startswith("//") else href
    parsed = urlparse(candidate)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return target or ""
    return candidate if parsed.scheme in ("http", "https") else ""
