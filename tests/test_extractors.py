from cute_web_scraper.extractors import (
    extract_by_selector,
    extract_emails,
    extract_links,
    extract_phones,
    extract_social_links,
)

PAGE = "https://example.com/contact"


def test_extract_email_with_context():
    results = extract_emails("<p>Contact us at hello@example.com for help.</p>", PAGE)
    assert len(results) == 1
    assert results[0]["value"] == "hello@example.com"
    assert results[0]["url"] == PAGE
    assert "Contact us" in results[0]["context"]


def test_extract_email_none():
    assert extract_emails("<p>No emails here</p>", PAGE) == []


def test_extract_email_deduplicates():
    assert len(extract_emails("<p>a@b.com</p><p>a@b.com</p>", PAGE)) == 1


def test_extract_international_phone():
    results = extract_phones("<p>Call us: +44 7700 900123</p>", PAGE)
    assert any("7700" in r["value"] for r in results)


def test_extract_uk_trunk_phone():
    results = extract_phones("<p>Tel: 020 7946 0958</p>", PAGE)
    assert any("7946" in r["value"] for r in results)


def test_year_and_order_numbers_are_not_phones():
    """Regression: v1's pattern matched dates, prices and order IDs."""
    html = "<p>&copy; 2024 Company Ltd. Order 1234 5678. Price 1,299.00</p>"
    assert extract_phones(html, PAGE) == []


def test_links_resolve_to_absolute():
    results = extract_links('<a href="/about">About</a>', PAGE)
    assert results[0]["value"] == "https://example.com/about"
    assert results[0]["context"] == "About"


def test_links_exclude_non_http_schemes():
    html = '<a href="mailto:a@b.com">Mail</a><a href="javascript:void(0)">JS</a>'
    assert extract_links(html, PAGE) == []


def test_extract_social_github():
    results = extract_social_links('<a href="https://github.com/maccydee">GH</a>', PAGE)
    assert len(results) == 1
    assert results[0]["platform"] == "github"
    assert "maccydee" in results[0]["value"]


def test_extract_social_none():
    assert extract_social_links("<p>No socials</p>", PAGE) == []


# ------------------------------------------------------------ selector extraction

LISTING = """
<html><body>
  <div class="card"><h2 class="title">Trail Runner</h2><span class="price">£129.99</span>
    <a class="more" href="/p/1">details</a></div>
  <div class="card"><h2 class="title">Road Runner</h2><span class="price">£99.00</span>
    <a class="more" href="/p/2">details</a></div>
  <div class="card"><h2 class="title">Track Spike</h2><span class="price">£75.50</span>
    <a class="more" href="/p/3">details</a></div>
</body></html>
"""


def test_row_selector_turns_a_listing_into_rows():
    rows = extract_by_selector(
        LISTING, PAGE, {"name": ".title", "price": ".price"}, row_selector=".card"
    )
    assert len(rows) == 3
    assert rows[0]["name"] == "Trail Runner"
    assert rows[0]["price"] == "£129.99"
    assert rows[2]["name"] == "Track Spike"


def test_attribute_syntax_reads_an_attribute():
    rows = extract_by_selector(
        LISTING, PAGE, {"name": ".title", "link": ".more@href"}, row_selector=".card"
    )
    assert rows[0]["link"] == "https://example.com/p/1"


def test_without_a_row_selector_the_page_is_one_row():
    rows = extract_by_selector(LISTING, PAGE, {"first": ".title"})
    assert len(rows) == 1
    assert rows[0]["first"] == "Trail Runner"


def test_missing_fields_come_back_empty_not_absent():
    rows = extract_by_selector(
        LISTING, PAGE, {"name": ".title", "rating": ".stars"}, row_selector=".card"
    )
    assert rows[0]["rating"] == ""
    assert rows[0]["name"] == "Trail Runner"


def test_rows_with_no_values_are_dropped():
    rows = extract_by_selector(LISTING, PAGE, {"nope": ".does-not-exist"}, row_selector=".card")
    assert rows == []


def test_an_invalid_selector_does_not_crash():
    rows = extract_by_selector(LISTING, PAGE, {"bad": "((("}, row_selector=".card")
    assert rows == []


def test_every_row_carries_its_source_url():
    rows = extract_by_selector(LISTING, PAGE, {"name": ".title"}, row_selector=".card")
    assert all(r["url"] == PAGE for r in rows)


def test_no_fields_returns_nothing():
    assert extract_by_selector(LISTING, PAGE, {}) == []
