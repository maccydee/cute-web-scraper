"""HTML cleanup and markdown conversion.

Two strategies, chosen per page:

* **Article extraction** (trafilatura) isolates the main body and drops navigation,
  cookie banners, sidebars and footers. On an independent 2,008-page benchmark it
  scores 0.791 F1 against Readability's 0.674, and neural readers came in slower
  *and* worse, so heuristics remain the right tool.
* **Whole-document conversion** keeps everything.

The split matters because trafilatura is tuned for prose. The same benchmark found
extractors diverge by 20-30 points on collection and product pages, where "main
content" is a grid rather than an article — so listing pages keep the full document
and let the dedicated extractors do the work.
"""

from __future__ import annotations

from bs4 import BeautifulSoup
from markdownify import markdownify

_DROP_TAGS = ["script", "style", "noscript", "template", "svg"]

_MIN_ARTICLE_CHARS = 200
"""Below this, treat trafilatura's output as a miss and keep the full document."""

_ARTICLE_MARKERS = ("<article", 'role="article"', "articleBody", "NewsArticle", "BlogPosting")


def page_title(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    if soup.title is None or soup.title.string is None:
        return ""
    return str(soup.title.string).strip()


def looks_like_an_article(html: str) -> bool:
    """Cheap check for prose-shaped pages, where article extraction pays off."""
    if not html:
        return False
    return any(marker in html for marker in _ARTICLE_MARKERS)


def extract_article(html: str) -> str | None:
    """Main body as markdown, or None when there is no clear article to isolate."""
    if not html:
        return None
    try:
        import trafilatura
    except ImportError:  # pragma: no cover - declared dependency
        return None
    try:
        extracted = trafilatura.extract(
            html,
            output_format="markdown",
            include_tables=True,
            include_links=True,
            favor_recall=True,
        )
    except Exception:  # noqa: BLE001 - never let extraction break a fetch
        return None
    if not extracted or len(extracted.strip()) < _MIN_ARTICLE_CHARS:
        # trafilatura returns None when it finds no main content, and a stub when
        # it guesses badly. Either way the full document is the safer answer.
        return None
    return extracted.strip()


def html_to_markdown(html: str, *, main_content: bool = False) -> str:
    """Convert to markdown, optionally isolating the main article body."""
    if not html:
        return ""
    if main_content:
        article = extract_article(html)
        if article is not None:
            return article
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(_DROP_TAGS):
        tag.decompose()
    if soup.head is not None:
        soup.head.decompose()
    converted: str = markdownify(str(soup), heading_style="ATX")
    return converted.strip()
