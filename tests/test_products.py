import pytest

from cute_web_scraper.products import extract_product

URL = "https://shop.example/p/1"

JSON_LD = """
<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product","name":"Trail Runner",
 "brand":{"@type":"Brand","name":"Fellrunner"},"sku":"TR-42","image":"https://img/x.jpg",
 "offers":{"@type":"Offer","price":"129.99","priceCurrency":"GBP",
           "availability":"https://schema.org/InStock"},
 "aggregateRating":{"@type":"AggregateRating","ratingValue":"4.6","reviewCount":"213"}}
</script></head><body>x</body></html>
"""

OPENGRAPH = """
<html><head>
<meta property="og:title" content="Wool Jumper"/>
<meta property="og:image" content="https://img/j.jpg"/>
<meta property="product:price:amount" content="79.00"/>
<meta property="product:price:currency" content="GBP"/>
<meta property="product:availability" content="in stock"/>
</head><body>x</body></html>
"""

MICRODATA = """
<html><body>
<div itemscope itemtype="http://schema.org/Product">
  <span itemprop="name">Canvas Tote</span>
  <span itemprop="sku">CT-7</span>
  <div itemprop="offers" itemscope itemtype="http://schema.org/Offer">
    <span itemprop="price">24.50</span>
    <meta itemprop="priceCurrency" content="USD"/>
  </div>
</div>
</body></html>
"""


def test_json_ld_full_extraction():
    p = extract_product(JSON_LD, URL)
    assert p["name"] == "Trail Runner"
    assert p["price"] == 129.99
    assert p["currency"] == "GBP"
    assert p["brand"] == "Fellrunner"
    assert p["sku"] == "TR-42"
    assert p["availability"] == "InStock"
    assert p["rating"] == 4.6
    assert p["review_count"] == 213
    assert p["image"] == "https://img/x.jpg"
    assert p["source"] == "json-ld"


def test_opengraph_fallback():
    p = extract_product(OPENGRAPH, URL)
    assert p["name"] == "Wool Jumper"
    assert p["price"] == 79.0
    assert p["currency"] == "GBP"
    assert p["availability"] == "in stock"
    assert p["source"] == "opengraph"


def test_microdata_fallback():
    p = extract_product(MICRODATA, URL)
    assert p["name"] == "Canvas Tote"
    assert p["price"] == 24.50
    assert p["currency"] == "USD"
    assert p["sku"] == "CT-7"
    assert p["source"] == "microdata"


def test_json_ld_wins_over_opengraph():
    """A page with both should prefer the richer, more reliable source."""
    combined = JSON_LD.replace(
        "</head>", OPENGRAPH.split("<head>")[1].split("</head>")[0] + "</head>"
    )
    p = extract_product(combined, URL)
    assert p["name"] == "Trail Runner"
    assert p["source"] == "json-ld"


def test_graph_wrapped_json_ld():
    """Many sites wrap entities in @graph rather than exposing Product at top level."""
    html = """<script type="application/ld+json">
    {"@context":"https://schema.org","@graph":[
      {"@type":"WebSite","name":"Shop"},
      {"@type":"Product","name":"Graph Widget","offers":{"price":"9.99","priceCurrency":"EUR"}}]}
    </script>"""
    p = extract_product(html, URL)
    assert p["name"] == "Graph Widget"
    assert p["price"] == 9.99


def test_array_of_entities():
    html = """<script type="application/ld+json">
    [{"@type":"BreadcrumbList"},{"@type":"Product","name":"Array Widget",
      "offers":{"price":"1.50","priceCurrency":"USD"}}]
    </script>"""
    p = extract_product(html, URL)
    assert p["name"] == "Array Widget"


def test_price_with_currency_symbol_and_commas():
    html = """<script type="application/ld+json">
    {"@type":"Product","name":"Pricey","offers":{"price":"$1,299.00","priceCurrency":"USD"}}
    </script>"""
    p = extract_product(html, URL)
    assert p["price"] == 1299.00


def test_offers_as_list_takes_first():
    html = """<script type="application/ld+json">
    {"@type":"Product","name":"Multi","offers":[
      {"price":"5.00","priceCurrency":"GBP"},{"price":"9.00","priceCurrency":"GBP"}]}
    </script>"""
    p = extract_product(html, URL)
    assert p["price"] == 5.00


def test_malformed_json_ld_does_not_crash():
    html = '<script type="application/ld+json">{not valid json,,,}</script><p>hi</p>'
    p = extract_product(html, URL)
    assert p["source"] == "none"
    assert p["name"] is None


def test_non_product_page_returns_empty_shape():
    p = extract_product("<html><body><p>Just an article</p></body></html>", URL)
    assert p["source"] == "none"
    assert p["url"] == URL
    assert set(p) >= {"url", "name", "price", "currency", "availability", "brand", "sku"}


def test_relative_image_is_absolutised():
    html = """<script type="application/ld+json">
    {"@type":"Product","name":"Rel","image":"/img/rel.jpg"}</script>"""
    p = extract_product(html, "https://shop.example/p/1")
    assert p["image"] == "https://shop.example/img/rel.jpg"


@pytest.mark.parametrize("junk", ["image/svg+xml", "⚑image/svg+xml", "Product photo", "⚑"])
def test_junk_image_values_are_discarded(junk):
    """Found live: a value of '⚑image/svg+xml' was absolutised into a nonsense URL."""
    html = f"""
    <div itemscope itemtype="http://schema.org/Product">
      <span itemprop="name">Nutella</span>
      <span itemprop="image">{junk}</span>
    </div>"""
    p = extract_product(html, "https://site.example/product/123")
    assert p["name"] == "Nutella"
    assert p["image"] is None


def test_linked_brand_uses_its_text_not_its_href():
    """Regression: reading href for text props turned the brand into '/facets/brands/X'."""
    html = """
    <div itemscope itemtype="http://schema.org/Product">
      <span itemprop="name">Spread</span>
      <a itemprop="brand" href="/facets/brands/nutella">Nutella</a>
    </div>"""
    p = extract_product(html, "https://site.example/p/1")
    assert p["brand"] == "Nutella"


def test_microdata_image_read_from_img_src():
    html = """
    <div itemscope itemtype="http://schema.org/Product">
      <span itemprop="name">Boots</span>
      <img itemprop="image" src="/media/boots.jpg" alt="a photo of boots"/>
    </div>"""
    p = extract_product(html, "https://site.example/p/1")
    assert p["image"] == "https://site.example/media/boots.jpg"


def test_relative_image_without_leading_slash_still_resolves():
    html = """<script type="application/ld+json">
    {"@type":"Product","name":"Rel2","image":"img/a.jpg"}</script>"""
    p = extract_product(html, "https://site.example/shop/p/1")
    assert p["image"] == "https://site.example/shop/p/img/a.jpg"


def test_image_list_takes_first():
    html = """<script type="application/ld+json">
    {"@type":"Product","name":"Imgs","image":["https://a/1.jpg","https://a/2.jpg"]}</script>"""
    assert extract_product(html, URL)["image"] == "https://a/1.jpg"
