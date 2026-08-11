"""MCP tool surface."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from mcp.server.mcpserver import MCPServer

from . import extractors
from .config import Config
from .products import extract_product
from .scraper import FetchResult, Scraper
from .shopify import ShopifyError, fetch_collections, fetch_store_products, variant_rows
from .store import ResultStore, StoreError

log = logging.getLogger(__name__)

ExtractorFn = Callable[[str, str], list[dict[str, str]]]

_DEFAULT_MAX_INLINE_CHARS = 50_000


class ScraperHolder:
    """Indirection so tools can be registered before the Scraper exists.

    MCP 2.0's Context is unavailable outside a live request, so a ctx-based lookup
    cannot be driven by call_tool in tests. A holder keeps production wiring honest
    via the entry point while leaving the real dispatch path testable.
    """

    def __init__(
        self,
        scraper: Scraper | None = None,
        store: ResultStore | None = None,
        config: Config | None = None,
    ) -> None:
        self._scraper: Any = scraper
        self._store = store
        self._config = config

    def set(
        self, scraper: Any, store: ResultStore | None = None, config: Config | None = None
    ) -> None:
        self._scraper = scraper
        if store is not None:
            self._store = store
        if config is not None:
            self._config = config

    def require(self) -> Any:
        if self._scraper is None:
            raise RuntimeError("Scraper is not initialised; the server lifespan did not run")
        return self._scraper

    def require_store(self) -> ResultStore:
        if self._store is None:
            raise RuntimeError("Result store is not initialised; the server lifespan did not run")
        return self._store

    @property
    def max_inline_chars(self) -> int:
        if self._config is None:
            return _DEFAULT_MAX_INLINE_CHARS
        return self._config.max_inline_chars


def create_server(holder: ScraperHolder) -> MCPServer:
    mcp = MCPServer("cute-web-scraper")
    _register_fetch_tools(mcp, holder)
    _register_discovery_tools(mcp, holder)
    _register_extract_tools(mcp, holder)
    _register_product_tools(mcp, holder)
    _register_shopify_tools(mcp, holder)
    _register_place_tools(mcp, holder)
    _register_table_tools(mcp, holder)
    _register_prompts(mcp)
    return mcp


# --------------------------------------------------------------------------- places


def _register_place_tools(mcp: MCPServer, holder: ScraperHolder) -> None:
    @mcp.tool(
        description=(
            "Search for places and local businesses by name or description — "
            "'the British Museum', 'cafes in Shoreditch'. Returns name, address, "
            "coordinates, phone, website, opening hours and category. "
            "Data comes from OpenStreetMap, so there are no star ratings or review "
            "counts; for those you would need a paid Google Places key. "
            "Pass save_as='<table_name>' to store the results."
        )
    )
    async def find_places(query: str, limit: int = 20, save_as: str = "") -> str:
        from .places import PlacesError, search_places

        scraper = holder.require()
        try:
            places = await search_places(query, scraper.http, limit=limit)
        except PlacesError as exc:
            return json.dumps({"query": query, "error": _describe(exc), "results": []})
        if save_as:
            return _save_and_summarise(holder, save_as, places, [])
        return _truncate(
            json.dumps(
                {"query": query, "count": len(places), "results": places}, ensure_ascii=False
            ),
            holder.max_inline_chars,
            hint="Re-run with save_as='<table_name>' and query it with query_table.",
        )

    @mcp.tool(
        description=(
            "Find every business of a category within a radius of a place — "
            "'dentists near Bath', 'cafes within 2km of Shoreditch'. This is the tool "
            "for local lead generation: it returns name, address, phone, website and "
            "opening hours for each. `category` accepts friendly names (cafe, dentist, "
            "hotel, solicitor, gym, hairdresser, ...) or a raw OpenStreetMap tag like "
            "'amenity=dentist'. Data is OpenStreetMap, so there are no star ratings. "
            "Pass save_as='<table_name>' to store the results."
        )
    )
    async def find_places_nearby(
        category: str,
        near: str,
        radius_m: int = 5000,
        limit: int = 100,
        save_as: str = "",
    ) -> str:
        from .places import PlacesError, find_nearby, geocode_one

        scraper = holder.require()
        try:
            lat, lon, label = await geocode_one(near, scraper.http)
            places = await find_nearby(
                category, lat, lon, scraper.http, radius_m=radius_m, limit=limit
            )
        except PlacesError as exc:
            return json.dumps(
                {"category": category, "near": near, "error": _describe(exc), "results": []}
            )

        named = [p for p in places if p.get("name")]
        if save_as and named:
            summary = json.loads(_save_and_summarise(holder, save_as, named, []))
            summary["centre"] = label
            summary["radius_m"] = radius_m
            return json.dumps(summary, ensure_ascii=False)
        return _truncate(
            json.dumps(
                {
                    "category": category,
                    "centre": label,
                    "radius_m": radius_m,
                    "count": len(named),
                    "results": named,
                },
                ensure_ascii=False,
            ),
            holder.max_inline_chars,
            hint="Re-run with save_as='<table_name>' and query it with query_table.",
        )


# --------------------------------------------------------------------------- fetch


def _register_fetch_tools(mcp: MCPServer, holder: ScraperHolder) -> None:
    @mcp.tool(
        description=(
            "Fetch one web page and return its content as clean markdown with metadata. "
            "Set js_render=true for pages that need JavaScript to render (SPAs, "
            "infinite-scroll listings, most modern storefronts)."
        )
    )
    async def fetch_page(url: str, js_render: bool = False) -> str:
        log.debug("tool=fetch_page url=%s js_render=%s", url, js_render)
        try:
            result = await holder.require().fetch(url, js_render=js_render)
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller as text
            log.error("tool=fetch_page failed: %s", exc)
            return f"Error fetching {url}: {_describe(exc)}"
        return _truncate(_render_page(result), holder.max_inline_chars)

    @mcp.tool(
        description=(
            "Fetch many web pages in parallel. Returns JSON with `results` and `errors`. "
            "Set js_render=true for JavaScript-heavy pages. "
            "For more than about 20 URLs, pass save_as='<table_name>' to write the pages "
            "into a result table and get back a summary instead of the full text — then "
            "use query_table to interrogate it without filling the conversation."
        )
    )
    async def fetch_pages(urls: list[str], js_render: bool = False, save_as: str = "") -> str:
        scraper = holder.require()
        results, errors = await _gather_pages(scraper, urls, js_render)
        rows = [
            {
                "url": r.url,
                "title": r.title,
                "status_code": r.status_code,
                "blocked": r.blocked,
                "block_reason": r.block_reason,
                "links_count": r.links_count,
                "markdown": r.markdown,
            }
            for r in results
        ]
        if save_as:
            return _save_and_summarise(holder, save_as, rows, errors)
        return _truncate(
            json.dumps({"results": rows, "errors": errors}, ensure_ascii=False),
            holder.max_inline_chars,
            hint="Re-run with save_as='<table_name>' and query it with query_table.",
        )


# ----------------------------------------------------------------------- discovery


def _register_discovery_tools(mcp: MCPServer, holder: ScraperHolder) -> None:
    @mcp.tool(
        description=(
            "Discover the pages on a website. Prefers the site's sitemap (following "
            "sitemap indexes and robots.txt), and falls back to following links. "
            "Returns JSON with `urls`, `count`, `source` and `truncated`. Run this "
            "before fetch_pages to scrape a whole site."
        )
    )
    async def crawl_site(url: str, limit: int = 500) -> str:
        from .crawler import crawl_by_links, discover_sitemap_urls

        scraper = holder.require()
        try:
            urls = await discover_sitemap_urls(url, scraper.http)
            source = "sitemap"
            if not urls:
                urls = await crawl_by_links(url, limit=limit, scraper=scraper)
                source = "links"
        except Exception as exc:  # noqa: BLE001
            log.error("tool=crawl_site failed: %s", exc)
            return json.dumps({"url": url, "error": _describe(exc), "urls": []})

        truncated = len(urls) > limit
        return json.dumps(
            {
                "url": url,
                "source": source,
                "count": len(urls[:limit]),
                "truncated": truncated,
                "urls": urls[:limit],
            },
            ensure_ascii=False,
        )

    @mcp.tool(
        description=(
            "Inspect a website before scraping it: detects the platform (Shopify, "
            "WordPress, Wix, ...), locates its sitemap, estimates how many pages it "
            "has, and reports whether JavaScript rendering is needed."
        )
    )
    async def analyze_website(url: str) -> str:
        from .crawler import detect_platform, detect_requires_js, discover_sitemap_urls

        scraper = holder.require()
        try:
            page = await scraper.fetch(url)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"url": url, "error": _describe(exc)})

        try:
            sitemap_urls = await discover_sitemap_urls(url, scraper.http)
        except Exception:  # noqa: BLE001
            sitemap_urls = []

        return json.dumps(
            {
                "url": url,
                "status_code": page.status_code,
                "blocked": page.blocked,
                "platform": detect_platform(page.html, {}),
                "sitemap_url": (urljoin(url, "/sitemap.xml") if sitemap_urls else None),
                "page_count_estimate": len(sitemap_urls),
                "requires_js": detect_requires_js(page.html),
            },
            ensure_ascii=False,
        )


# ------------------------------------------------------------------------- extract


def _register_extract_tools(mcp: MCPServer, holder: ScraperHolder) -> None:
    specs: list[tuple[str, ExtractorFn, str]] = [
        (
            "extract_emails",
            extractors.extract_emails,
            "Scan a list of URLs for email addresses. Returns JSON with `results` "
            "({url, value, context}) and `errors`.",
        ),
        (
            "extract_phones",
            extractors.extract_phones,
            "Scan a list of URLs for phone numbers. Returns JSON with `results` "
            "({url, value, context}) and `errors`.",
        ),
        (
            "extract_links",
            extractors.extract_links,
            "Collect every hyperlink from a list of URLs, resolved to absolute URLs. "
            "Returns JSON with `results` ({url, value, context}) and `errors`.",
        ),
        (
            "extract_social_links",
            extractors.extract_social_links,
            "Find social media profile links (LinkedIn, X, Facebook, Instagram, "
            "YouTube, TikTok, GitHub, Pinterest) across a list of URLs. Returns JSON "
            "with `results` ({url, platform, value}) and `errors`.",
        ),
    ]
    shared = " Pass save_as='<table_name>' to store results instead of returning them inline."
    for name, fn, description in specs:
        mcp.add_tool(_build_extract_tool(holder, fn), name=name, description=description + shared)


def _build_extract_tool(holder: ScraperHolder, extractor: ExtractorFn) -> Callable[..., Any]:
    # A plain closure registered via add_tool. functools.wraps would copy the
    # extractor's __annotations__/__wrapped__ onto this function and publish a
    # wrong tool schema.
    async def _extract(urls: list[str], js_render: bool = False, save_as: str = "") -> str:
        scraper = holder.require()
        pages, errors = await _gather_pages(scraper, urls, js_render)
        results: list[dict[str, Any]] = []
        for page in pages:
            # Raw HTML, never markdown. Link and social extraction parse <a href>
            # tags, which markdown does not contain.
            results.extend(extractor(page.html, page.url))
        if save_as:
            return _save_and_summarise(holder, save_as, results, errors)
        return _truncate(
            json.dumps({"results": results, "errors": errors}, ensure_ascii=False),
            holder.max_inline_chars,
            hint="Re-run with save_as='<table_name>' and query it with query_table.",
        )

    return _extract


def _register_product_tools(mcp: MCPServer, holder: ScraperHolder) -> None:
    @mcp.tool(
        description=(
            "Extract structured product data (name, price, currency, availability, brand, "
            "sku, image, rating, review_count) from a list of product URLs. Reads the "
            "page's own JSON-LD, OpenGraph or microdata rather than guessing at selectors, "
            "so it works across most storefronts without configuration. "
            "Pass save_as='<table_name>' to store the rows for querying."
        )
    )
    async def extract_products(urls: list[str], js_render: bool = False, save_as: str = "") -> str:
        scraper = holder.require()
        pages, errors = await _gather_pages(scraper, urls, js_render)
        rows = [extract_product(page.html, page.url) for page in pages]
        found = [r for r in rows if r["source"] != "none"]
        for row in rows:
            if row["source"] == "none":
                errors.append(
                    {"url": str(row["url"]), "error": "no structured product data on this page"}
                )
        if save_as:
            return _save_and_summarise(holder, save_as, found, errors)
        return _truncate(
            json.dumps({"results": found, "errors": errors}, ensure_ascii=False),
            holder.max_inline_chars,
            hint="Re-run with save_as='<table_name>' and query it with query_table.",
        )


# ------------------------------------------------------------------------- shopify


def _register_shopify_tools(mcp: MCPServer, holder: ScraperHolder) -> None:
    @mcp.tool(
        description=(
            "List a Shopify store's collections with their product counts. Use this to "
            "pick which collections to extract before calling extract_shopify_store."
        )
    )
    async def list_shopify_collections(store_url: str) -> str:
        scraper = holder.require()
        try:
            collections = await fetch_collections(store_url, scraper.http)
        except ShopifyError as exc:
            return json.dumps({"store_url": store_url, "error": _describe(exc)})
        return json.dumps(
            {"store_url": store_url, "count": len(collections), "collections": collections},
            ensure_ascii=False,
        )

    @mcp.tool(
        description=(
            "Extract a Shopify store's catalogue as one row per product variant — price, "
            "sku, options, availability, vendor, image and product URL. Reads the store's "
            "public products.json, so it needs no rendering and no selectors. "
            "Pass save_as='<table_name>' to store the rows (recommended: catalogues are "
            "large). max_products caps how many products are pulled."
        )
    )
    async def extract_shopify_store(
        store_url: str, save_as: str = "", max_products: int = 0
    ) -> str:
        scraper = holder.require()
        try:
            products = await fetch_store_products(
                store_url, scraper.http, max_products=max_products or None
            )
        except ShopifyError as exc:
            return json.dumps({"store_url": store_url, "error": _describe(exc)})

        rows = variant_rows(products, store_url)
        if not rows:
            return json.dumps({"store_url": store_url, "products": 0, "variants": 0, "results": []})
        if save_as:
            summary = json.loads(_save_and_summarise(holder, save_as, rows, []))
            summary["products"] = len(products)
            summary["variants"] = len(rows)
            return json.dumps(summary, ensure_ascii=False)
        return _truncate(
            json.dumps(
                {
                    "store_url": store_url,
                    "products": len(products),
                    "variants": len(rows),
                    "results": rows,
                },
                ensure_ascii=False,
            ),
            holder.max_inline_chars,
            hint="Re-run with save_as='<table_name>' and query it with query_table.",
        )


# -------------------------------------------------------------------------- tables


def _register_table_tools(mcp: MCPServer, holder: ScraperHolder) -> None:
    @mcp.tool(
        description=(
            "List saved result tables with their row counts and columns. Result tables are "
            "produced by any tool called with save_as."
        )
    )
    async def list_tables() -> str:
        tables = holder.require_store().list_tables()
        return json.dumps(
            {
                "count": len(tables),
                "tables": [
                    {"name": t.name, "row_count": t.row_count, "columns": t.columns} for t in tables
                ],
            },
            ensure_ascii=False,
        )

    @mcp.tool(
        description=(
            "Inspect one result table: its columns, row count, and a small sample of rows."
        )
    )
    async def get_table(name: str, sample: int = 5) -> str:
        try:
            info = holder.require_store().get_table(name, sample=sample)
        except StoreError as exc:
            return json.dumps({"name": name, "error": _describe(exc)})
        return _truncate(
            json.dumps(
                {
                    "name": info.name,
                    "row_count": info.row_count,
                    "columns": info.columns,
                    "sample": info.sample,
                },
                ensure_ascii=False,
            ),
            holder.max_inline_chars,
        )

    @mcp.tool(
        description=(
            "Run a read-only SQL SELECT against saved result tables. This is how you "
            "analyse a large scrape without pulling it into the conversation: filter, "
            "aggregate, group and sort a table of any size and get back only the rows you "
            "asked for. Only SELECT is permitted — the query can never modify saved data. "
            "Example: SELECT vendor, COUNT(*) AS n, AVG(price) AS avg_price FROM catalogue "
            "GROUP BY vendor ORDER BY n DESC.\n\n"
            "Pass save_as='<table_name>' to persist the query's result as a new table. "
            "That is how you clean data here: SELECT DISTINCT deduplicates, aliases rename "
            "columns, `a || ', ' || b AS c` merges them, and WHERE drops unwanted rows — "
            "all in one step, with the original left untouched unless you deliberately "
            "target its name."
        )
    )
    async def query_table(sql: str, max_rows: int = 200, save_as: str = "") -> str:
        store = holder.require_store()
        if save_as:
            try:
                info, replaced, truncated = store.query_to_table(sql, save_as)
            except StoreError as exc:
                return json.dumps({"error": _describe(exc)})
            return json.dumps(
                {
                    "saved_to": info.name,
                    "row_count": info.row_count,
                    "columns": info.columns,
                    # Surfaced so a filter that overwrote its own source is visible
                    # rather than a silent loss of rows.
                    "replaced_existing_table": replaced,
                    "truncated": truncated,
                    "sample": _shrink_sample(store.get_table(info.name, sample=3).sample),
                },
                ensure_ascii=False,
            )
        try:
            result = store.query(sql, max_rows=max_rows)
        except StoreError as exc:
            return json.dumps({"error": _describe(exc)})
        return _truncate(
            json.dumps(
                {
                    "columns": result.columns,
                    "row_count": len(result.rows),
                    "truncated": result.truncated,
                    "rows": result.rows,
                },
                ensure_ascii=False,
            ),
            holder.max_inline_chars,
            hint="Narrow the query with WHERE, GROUP BY or a smaller max_rows.",
        )

    @mcp.tool(
        description=(
            "Export a result table to a file on disk as CSV or JSON, and return its path. "
            "Use this to hand data to a spreadsheet or another tool."
        )
    )
    async def export_table(name: str, fmt: str = "csv", dest_dir: str = "") -> str:
        target_dir = Path(dest_dir).expanduser() if dest_dir else Path.home() / "Downloads"
        try:
            path = holder.require_store().export(name, fmt, target_dir)
        except StoreError as exc:
            return json.dumps({"name": name, "error": _describe(exc)})
        return json.dumps(
            {"name": name, "format": fmt.lower(), "path": str(path), "bytes": path.stat().st_size}
        )

    @mcp.tool(
        description=(
            "Delete a saved result table. This permanently removes the stored rows; the "
            "scraped pages themselves are unaffected."
        )
    )
    async def drop_table(name: str) -> str:
        try:
            info = holder.require_store().get_table(name, sample=0)
        except StoreError as exc:
            return json.dumps({"name": name, "error": _describe(exc)})
        holder.require_store().drop(name)
        return json.dumps({"name": name, "dropped": True, "rows_deleted": info.row_count})


# ------------------------------------------------------------------------- prompts


def _register_prompts(mcp: MCPServer) -> None:
    @mcp.prompt(
        name="scrape_site",
        description="Discover a whole site and scrape it into a queryable table.",
    )
    def scrape_site(url: str, table_name: str = "pages") -> str:
        return (
            f"Scrape the site at {url}.\n\n"
            f"1. Call analyze_website on {url} to see the platform, sitemap and whether "
            "JavaScript rendering is needed.\n"
            f"2. Call crawl_site to discover its URLs.\n"
            f"3. Call fetch_pages on those URLs with save_as='{table_name}' so the content "
            "goes into a result table rather than the conversation.\n"
            f"4. Report the row count, then use query_table to answer questions about it.\n\n"
            "Use js_render=true only if analyze_website says the site needs it."
        )

    @mcp.prompt(
        name="scrape_shopify_store",
        description="Export a Shopify store's catalogue, one row per variant.",
    )
    def scrape_shopify_store(store_url: str, table_name: str = "catalogue") -> str:
        return (
            f"Export the Shopify catalogue at {store_url}.\n\n"
            f"1. Call list_shopify_collections on {store_url} and show me the collections.\n"
            f"2. Call extract_shopify_store with save_as='{table_name}'.\n"
            "3. Report how many products and variants were captured.\n"
            "4. Summarise the catalogue with query_table — price range, vendors, and how "
            "many variants are out of stock.\n\n"
            f"If {store_url} is not a Shopify store, say so and suggest crawl_site instead."
        )

    @mcp.prompt(
        name="find_contacts",
        description="Find email addresses, phone numbers and social profiles for a site.",
    )
    def find_contacts(url: str, table_name: str = "contacts") -> str:
        return (
            f"Find contact details for {url}.\n\n"
            f"1. Call crawl_site on {url} with a limit of about 50 pages.\n"
            "2. Prefer URLs that look like contact, about, team, or support pages.\n"
            f"3. Call extract_emails, extract_phones and extract_social_links on them, "
            f"each with save_as (for example '{table_name}_emails').\n"
            "4. Report what you found, grouped by type, and flag anything that looks like "
            "a generic or boilerplate address rather than a real contact."
        )

    @mcp.prompt(
        name="compare_prices",
        description="Scrape product pages into a table and compare prices.",
    )
    def compare_prices(urls: str, table_name: str = "products") -> str:
        return (
            f"Compare prices for these product pages: {urls}\n\n"
            f"1. Call extract_products on them with save_as='{table_name}'.\n"
            "2. If any page returns no structured data, retry just those with "
            "js_render=true.\n"
            "3. Use query_table to sort by price and show me the results as a table, "
            "cheapest first, noting anything out of stock.\n"
            "4. Say explicitly which pages yielded no product data, rather than "
            "quietly leaving them out."
        )


# -------------------------------------------------------------------------- helpers


async def _gather_pages(
    scraper: Any, urls: list[str], js_render: bool
) -> tuple[list[FetchResult], list[dict[str, str]]]:
    """Fetch every URL, splitting successes from failures and blocks."""
    settled = await asyncio.gather(
        *(scraper.fetch(u, js_render=js_render) for u in urls),
        return_exceptions=True,
    )
    pages: list[FetchResult] = []
    errors: list[dict[str, str]] = []
    for url, outcome in zip(urls, settled, strict=True):
        if isinstance(outcome, BaseException):
            errors.append({"url": url, "error": _describe(outcome)})
        elif outcome.blocked:
            errors.append({"url": url, "error": f"blocked: {outcome.block_reason}"})
        else:
            pages.append(outcome)
    return pages, errors


def _save_and_summarise(
    holder: ScraperHolder,
    table: str,
    rows: list[dict[str, Any]],
    errors: list[dict[str, str]],
) -> str:
    """Write rows to a table and return a compact summary instead of the data."""
    if not rows:
        return json.dumps(
            {"saved_to": table, "row_count": 0, "errors": errors, "results": []},
            ensure_ascii=False,
        )
    try:
        info = holder.require_store().save(table, rows)
    except StoreError as exc:
        return json.dumps({"error": _describe(exc), "errors": errors})
    sample = holder.require_store().get_table(info.name, sample=3).sample
    return json.dumps(
        {
            "saved_to": info.name,
            "row_count": info.row_count,
            "columns": info.columns,
            "sample": _shrink_sample(sample),
            "errors": errors,
            "next": (f"Use query_table to analyse this, e.g. SELECT * FROM {info.name} LIMIT 20"),
        },
        ensure_ascii=False,
    )


def _shrink_sample(rows: list[dict[str, Any]], field_cap: int = 200) -> list[dict[str, Any]]:
    """Trim long fields so a sample never becomes the payload it was meant to avoid."""
    shrunk: list[dict[str, Any]] = []
    for row in rows:
        item: dict[str, Any] = {}
        for key, value in row.items():
            if isinstance(value, str) and len(value) > field_cap:
                item[key] = value[:field_cap] + f"… (+{len(value) - field_cap} chars)"
            else:
                item[key] = value
        shrunk.append(item)
    return shrunk


def _describe(exc: BaseException) -> str:
    """A readable description of a failure, even when the exception carries no message.

    httpx raises ReadTimeout('') among others, so str(exc) alone produces an empty
    string and an error like "Error fetching https://...: " that says nothing.
    """
    message = str(exc).strip()
    name = type(exc).__name__
    return f"{name}: {message}" if message else name


def _truncate(payload: str, max_chars: int, hint: str = "") -> str:
    """Cap a tool's inline output so one call cannot fill the conversation."""
    if len(payload) <= max_chars:
        return payload
    suffix = f"\n\n[truncated: {len(payload)} chars exceeded the {max_chars}-char limit."
    if hint:
        suffix += f" {hint}"
    suffix += "]"
    return payload[:max_chars] + suffix


def _render_page(result: FetchResult) -> str:
    front_matter = "\n".join(
        [
            "---",
            f"url: {result.url}",
            f"status_code: {result.status_code}",
            f"title: {result.title}",
            f"links_count: {result.links_count}",
            f"blocked: {str(result.blocked).lower()}",
            f"block_reason: {result.block_reason or ''}",
            "---",
        ]
    )
    if result.blocked:
        return (
            f"{front_matter}\n\n"
            f"This page was blocked ({result.block_reason}). The rate limiter has "
            "increased the delay for this domain; retrying later may succeed. "
            "For challenge pages, try js_render=true."
        )
    return f"{front_matter}\n\n{result.markdown}"
