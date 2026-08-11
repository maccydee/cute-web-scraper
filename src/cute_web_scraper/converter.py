"""HTML cleanup and markdown conversion."""

from __future__ import annotations

from bs4 import BeautifulSoup
from markdownify import markdownify

_DROP_TAGS = ["script", "style", "noscript", "template", "svg"]


def page_title(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    if soup.title is None or soup.title.string is None:
        return ""
    return str(soup.title.string).strip()


def html_to_markdown(html: str) -> str:
    """Strip non-content tags, then convert to markdown."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(_DROP_TAGS):
        tag.decompose()
    if soup.head is not None:
        soup.head.decompose()
    converted: str = markdownify(str(soup), heading_style="ATX")
    return converted.strip()
