import pytest

from cute_web_scraper.search import SearchError, parse_results, search_url

RESULTS_HTML = """
<html><body>
<a class="result-link" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fone&rut=x">
  First Result</a>
<div class="result-snippet">A description of the first result.</div>
<a class="result-link" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Ftwo">
  Second Result</a>
<div class="result-snippet">A description of the second result.</div>
<a class="result-link" href="https://direct.example/three">Third Result</a>
<div class="result-snippet">Third description.</div>
</body></html>
"""


def test_search_url_encodes_the_query():
    assert "engineering+manager" in search_url("engineering manager")


def test_empty_query_rejected():
    with pytest.raises(SearchError):
        search_url("   ")


def test_results_are_ranked_with_titles_and_snippets():
    results = parse_results(RESULTS_HTML)
    assert len(results) == 3
    assert results[0]["rank"] == 1
    assert results[0]["title"] == "First Result"
    assert results[0]["snippet"] == "A description of the first result."


def test_redirect_urls_are_unwrapped():
    """Results link through duckduckgo.com/l/?uddg=..., which is useless to fetch."""
    results = parse_results(RESULTS_HTML)
    assert results[0]["url"] == "https://example.com/one"
    assert results[1]["url"] == "https://example.org/two"


def test_direct_urls_pass_through():
    assert parse_results(RESULTS_HTML)[2]["url"] == "https://direct.example/three"


def test_limit_is_respected():
    assert len(parse_results(RESULTS_HTML, limit=2)) == 2


def test_duplicates_are_dropped():
    doubled = RESULTS_HTML + RESULTS_HTML
    urls = [r["url"] for r in parse_results(doubled, limit=50)]
    assert len(urls) == len(set(urls))


def test_empty_page_yields_nothing():
    assert parse_results("") == []
    assert parse_results("<html><body>no results</body></html>") == []
