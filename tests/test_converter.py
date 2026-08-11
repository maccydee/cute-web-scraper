from cute_web_scraper.converter import html_to_markdown, page_title


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
