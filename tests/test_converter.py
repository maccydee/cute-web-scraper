from cute_web_scraper.converter import (
    extract_article,
    html_to_markdown,
    looks_like_an_article,
    page_title,
)


def test_headings_and_body():
    md = html_to_markdown("<h1>Hello</h1><p>World</p>")
    assert "# Hello" in md
    assert "World" in md


def test_strips_scripts():
    md = html_to_markdown("<script>alert(1)</script><p>Keep</p>")
    assert "alert" not in md
    assert "Keep" in md


def test_strips_styles():
    md = html_to_markdown("<style>body{color:red}</style><p>text</p>")
    assert "color" not in md
    assert "text" in md


def test_preserves_link_targets():
    md = html_to_markdown('<a href="https://x.com">link</a>')
    assert "https://x.com" in md


def test_empty_input():
    assert html_to_markdown("") == ""


def test_page_title():
    assert page_title("<html><head><title>My Page</title></head><body>x</body></html>") == "My Page"


def test_page_title_missing():
    assert page_title("<html><body>x</body></html>") == ""


# --------------------------------------------------------- article extraction

ARTICLE_PAGE = """
<html><head><title>Real Article</title></head><body>
  <nav><a href="/a">Home</a><a href="/b">News</a><a href="/c">Sport</a></nav>
  <div id="cookie-banner">We use cookies. Accept all? Manage preferences.</div>
  <article>
    <h1>The Headline That Matters</h1>
    <p>{body}</p>
  </article>
  <footer><a href="/t">Terms</a><a href="/p">Privacy</a>Copyright 2026</footer>
</body></html>
""".format(body="This is the genuine article body text that a reader came for. " * 40)


def test_article_extraction_drops_boilerplate():
    md = html_to_markdown(ARTICLE_PAGE, main_content=True)
    assert "genuine article body text" in md
    assert "cookies" not in md.lower()
    assert "Copyright 2026" not in md


def test_article_extraction_is_much_smaller_than_the_full_document():
    full = html_to_markdown(ARTICLE_PAGE)
    main = html_to_markdown(ARTICLE_PAGE, main_content=True)
    assert len(main) < len(full)


def test_full_document_is_the_default():
    md = html_to_markdown(ARTICLE_PAGE)
    assert "Terms" in md, "without main_content the whole page is converted"


def test_a_listing_page_falls_back_to_the_full_document():
    """Trafilatura is tuned for prose; on a product grid it finds no article, and
    the fallback must return the whole page rather than nothing."""
    listing = (
        "<html><body><div class='grid'>"
        + "".join(f'<a href="/p/{i}">Product {i} £{i}.00</a>' for i in range(40))
        + "</div></body></html>"
    )
    md = html_to_markdown(listing, main_content=True)
    assert "Product 7" in md


def test_empty_input_with_main_content():
    assert html_to_markdown("", main_content=True) == ""


def test_looks_like_an_article():
    assert looks_like_an_article(ARTICLE_PAGE) is True
    assert looks_like_an_article("<html><body><div>grid</div></body></html>") is False


def test_extract_article_returns_none_on_a_stub():
    assert extract_article("<html><body><p>Tiny.</p></body></html>") is None
