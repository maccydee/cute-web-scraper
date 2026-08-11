from cute_web_scraper.extractors import (
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
