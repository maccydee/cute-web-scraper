import httpx

from cute_web_scraper.crawler import (
    crawl_by_links,
    detect_platform,
    detect_requires_js,
    discover_sitemap_urls,
)

URLSET = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/a</loc></url>
  <url><loc>https://example.com/b</loc></url>
</urlset>"""

INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/sitemap-1.xml</loc></sitemap>
</sitemapindex>"""

CHILD = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/product/1</loc></url>
</urlset>"""


async def test_plain_sitemap(httpx_mock):
    httpx_mock.add_response(url="https://example.com/sitemap.xml", text=URLSET)
    async with httpx.AsyncClient() as client:
        urls = await discover_sitemap_urls("https://example.com", client)
    assert urls == ["https://example.com/a", "https://example.com/b"]


async def test_sitemap_index_is_followed(httpx_mock):
    """Regression: v1 returned child sitemap URLs as if they were pages."""
    httpx_mock.add_response(url="https://example.com/sitemap.xml", text=INDEX)
    httpx_mock.add_response(url="https://example.com/sitemap-1.xml", text=CHILD)
    async with httpx.AsyncClient() as client:
        urls = await discover_sitemap_urls("https://example.com", client)
    assert urls == ["https://example.com/product/1"]
    assert "https://example.com/sitemap-1.xml" not in urls


async def test_robots_txt_sitemap_directive(httpx_mock):
    httpx_mock.add_response(url="https://example.com/sitemap.xml", status_code=404)
    httpx_mock.add_response(
        url="https://example.com/robots.txt",
        text="User-agent: *\nSitemap: https://example.com/custom-sitemap.xml\n",
    )
    httpx_mock.add_response(url="https://example.com/custom-sitemap.xml", text=URLSET)
    async with httpx.AsyncClient() as client:
        urls = await discover_sitemap_urls("https://example.com", client)
    assert "https://example.com/a" in urls


async def test_no_sitemap_returns_empty(httpx_mock):
    """Both discovery routes are tried and both come back empty."""
    httpx_mock.add_response(url="https://example.com/sitemap.xml", status_code=404)
    httpx_mock.add_response(url="https://example.com/robots.txt", status_code=404)
    async with httpx.AsyncClient() as client:
        assert await discover_sitemap_urls("https://example.com", client) == []


async def test_link_crawl_respects_limit_and_domain():
    fetched: list[str] = []

    class FakeResult:
        html = '<a href="/x">x</a><a href="/y">y</a><a href="https://other.com/z">z</a>'
        blocked = False

    class FakeScraper:
        async def fetch(self, url, *, js_render=False):
            fetched.append(url)
            return FakeResult()

    urls = await crawl_by_links("https://example.com", limit=2, scraper=FakeScraper())
    assert len(urls) <= 2
    assert all("example.com" in u for u in urls)
    assert len(fetched) <= 2


def test_detect_wordpress():
    assert detect_platform('<meta name="generator" content="WordPress 6.5">', {}) == "wordpress"


def test_detect_wix_from_header_name():
    """Regression: v1 searched header values only, so an X-Wix-* name never matched."""
    assert detect_platform("<html></html>", {"X-Wix-Request-Id": "abc123"}) == "wix"


def test_detect_requires_js():
    assert detect_requires_js('<script id="__NEXT_DATA__">{}</script>') is True
    assert detect_requires_js("<p>plain</p>") is False
