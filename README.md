# cute-web-scraper

An MCP server that gives Claude web scraping powers.

Ask Claude to scrape a site in plain English. It fetches the pages, renders the JavaScript when needed, and hands back clean markdown or structured data — no selectors, no glue code.

- **Whole sites, not single pages.** Discover every URL from a sitemap, then fetch them in parallel.
- **Contacts and links.** Pull emails, phone numbers, hyperlinks and social profiles from a list of URLs.
- **Markdown, not HTML.** Pages come back as clean markdown, with navigation, cookie banners and footers stripped from articles. A BBC news page drops from 20,519 characters to 3,198.
- **PDFs too.** A link to a PDF is extracted to text rather than silently skipped.

## Install

```bash
pipx install git+https://github.com/maccydee/cute-web-scraper
```

Chromium is downloaded automatically the first time you use `js_render` (a one-off ~130MB).

## Connect it to Claude Code

```bash
claude mcp add cute-web-scraper -- cute-web-scraper
```

Then just ask:

```
Scrape every product from https://example-shop.com and give me a CSV of name and price.
```

## Tools

**Fetching and discovery**

| Tool | What it does |
|---|---|
| `fetch_page` | One URL to clean markdown, with title, status and link count |
| `fetch_pages` | Many URLs in parallel, returning results and per-URL errors |
| `crawl_site` | Discover a site's pages via sitemap, falling back to link-following |
| `analyze_website` | Detect the platform, find the sitemap, report whether JS is needed |

**Extraction**

| Tool | What it does |
|---|---|
| `extract_by_selector` | Arbitrary fields via CSS selectors — turns any listing into a table |
| `extract_products` | Structured product data (name, price, currency, availability, brand, sku, rating) from JSON-LD, OpenGraph or microdata |
| `extract_emails` | Email addresses across a list of URLs, with surrounding context |
| `extract_phones` | Phone numbers across a list of URLs, with surrounding context |
| `extract_links` | Every hyperlink, resolved to absolute URLs |
| `extract_social_links` | Social profiles across eight platforms |
| `extract_shopify_store` | A whole Shopify catalogue, one row per variant |
| `list_shopify_collections` | A Shopify store's collections and their product counts |

**Places and local businesses**

| Tool | What it does |
|---|---|
| `find_places` | Search by name or description — name, address, coordinates, phone, website, opening hours |
| `find_places_nearby` | Every business of a category within a radius of a place |

**Result tables**

| Tool | What it does |
|---|---|
| `list_tables` | Saved result tables with row counts and columns |
| `get_table` | One table's columns, row count and a sample |
| `query_table` | Read-only SQL over a saved table — filter, aggregate, group, sort, and optionally save the result as a new table |
| `export_table` | Write a table to CSV or JSON on disk |
| `drop_table` | Delete a saved table |

A typical run composes them: `analyze_website` → `crawl_site` → `fetch_pages` → `query_table`.

### Extracting arbitrary fields

`extract_by_selector` covers everything the fixed extractors do not:

```
Get the title, price and link from every product on these 40 pages,
save it as `catalogue`, then show me anything under £50.
```

`fields` maps column names to CSS selectors. `row_selector` makes each match a row — that is what turns a listing into a table. An `@attr` suffix reads an attribute instead of text, with `href` and `src` resolved to absolute URLs:

```json
{"name": "h3 a@title", "price": ".price_color", "link": "h3 a@href"}
```

### Slash commands

The server ships four ready-made workflows, which appear as slash commands in Claude Code: `scrape_site`, `scrape_shopify_store`, `find_contacts` and `compare_prices`.

## Working with large scrapes

Any tool that returns rows accepts `save_as`. Instead of putting the data in the conversation, it writes a result table and hands back a summary:

```
Extract the whole catalogue from deathwishcoffee.com into a table called `catalogue`,
then tell me the price range and how many variants are out of stock.
```

Claude calls `extract_shopify_store(save_as="catalogue")`, gets back a row count and column list, and then answers with `query_table`:

```sql
SELECT COUNT(*) AS variants, MIN(price) AS cheapest,
       MAX(price) AS dearest, SUM(available) AS in_stock
FROM catalogue
```

The table can hold 100,000 rows and none of them enter the conversation. `query_table` is strictly read-only — it runs against a read-only SQLite handle and rejects anything that is not a `SELECT`, so a query can never modify or delete saved data.

Tables live in a SQLite file at `~/.cute-web-scraper/results.db` (set `SCRAPER_DB_PATH` to move it).

### Cleaning data

`query_table` also takes `save_as`, which persists the result as a new table. SQL already expresses the usual cleanup operations, so there's no separate set of edit tools:

```sql
SELECT DISTINCT * FROM leads                                  -- deduplicate
SELECT street || ', ' || city AS address FROM leads           -- merge columns
SELECT name, phone FROM leads WHERE phone IS NOT NULL         -- drop columns and rows
SELECT vendor AS brand FROM catalogue                         -- rename
```

The source table is left untouched unless you deliberately target its own name, and the response says `replaced_existing_table` when you do — so an in-place filter is never a silent loss of rows.

## Places and local businesses

`find_places` looks up a single place; `find_places_nearby` returns everything of a category within a radius, which is the local lead-generation case:

```
Find every dentist within 4km of Bath, save it as `leads`,
then tell me how many have a website but no phone number.
```

Categories accept friendly names (`cafe`, `dentist`, `hotel`, `solicitor`, `gym`, `hairdresser`, …) or a raw OpenStreetMap tag like `amenity=dentist`.

**A note on the data source.** This is OpenStreetMap, not Google Maps. Google was the obvious target and it does not work: an automated browser gets a cookie-consent interstitial, and once past that, a degraded map shell with no place panel. The stealth tier does not help, because this is a consent wall rather than bot detection — a different problem from the one stealth solves.

OpenStreetMap gives the same fields — name, address, coordinates, phone, website, opening hours, category — through documented open endpoints with no key. The one thing it has no equivalent for is **star ratings and review counts**, which are Google's own proprietary data.

Both endpoints are volunteer-run. Nominatim's policy of one request per second is enforced internally regardless of `SCRAPER_DELAY_MS`, and Overpass queries fall through several public mirrors, because the main instance regularly returns 504 under load.

Tool output is also capped at `SCRAPER_MAX_INLINE_CHARS` (25,000 by default). Past that, a result is truncated with a note pointing at `save_as` — so a single call can't fill your context by accident.

## Example prompts

```
Export the whole catalogue from deathwishcoffee.com and tell me the price range.

Find all email addresses on https://company.com and its contact pages.

What platform is https://myblog.com on? Does it need JavaScript to scrape?

Scrape these 200 product pages into a table, then show me everything under £50 that's in stock.

Extract the social media links from these 10 agency sites: [urls...]
```

## Configuration

Everything is an environment variable, with defaults that work unconfigured.

| Variable | Default | Meaning |
|---|---|---|
| `SCRAPER_DELAY_MS` | `1000` | Base delay between requests to the same domain |
| `SCRAPER_MAX_CONCURRENT` | `5` | Maximum parallel requests |
| `SCRAPER_CACHE_TTL_S` | `300` | How long a fetched page stays reusable |
| `SCRAPER_CACHE_MAX_ENTRIES` | `500` | Cached pages before least-recently-used eviction |
| `SCRAPER_AUTH_TOKEN` | unset | Bearer token for HTTP mode |
| `SCRAPER_CHROME_USER_DATA_DIR` | unset | Chrome profile to inherit logged-in sessions from |
| `SCRAPER_IMPERSONATE` | `1` | Retry blocked requests with browser TLS fingerprints |
| `SCRAPER_STEALTH` | `1` | Last-resort stealth browser for the hardest blocks |
| `SCRAPER_DB_PATH` | `~/.cute-web-scraper/results.db` | Where result tables are stored |
| `SCRAPER_MAX_INLINE_CHARS` | `25000` | Ceiling on how much a single tool returns inline |

Batching a long URL list into one table needs `mode: "append"` on every call after the first, or each batch replaces the last. Rendered pages that come back sparse can be given `wait_ms`, or better `wait_for` with a CSS selector.

## How it behaves

**Main content, not the whole page.** Article-shaped pages are run through trafilatura, which isolates the body and drops the surrounding furniture — chosen because on an independent 2,008-page benchmark it scores 0.791 F1 against Readability's 0.674. It is applied per page rather than universally: the same benchmark shows extractors diverging by 20–30 points on product grids and collections, where "main content" is not an article, so listing pages keep the full document. Pass `main_content: false` to force that anywhere.

**Four tiers, escalating only when refused.** A plain HTTP client handles most pages. If a site refuses, the request retries with real browser TLS fingerprints (Chrome, then Safari), because some sites fingerprint the TLS handshake itself and no header change gets past them. `js_render: true` renders in Chromium for single-page apps. As a last resort, a stealth-patched browser handles sites that need JavaScript *and* reject ordinary automation.

Each tier fixes a different failure, and none is a superset of the others: the TLS tier can't run JavaScript, and Playwright is a detectably automated browser. Every result reports which tier served it. Set `SCRAPER_IMPERSONATE=0` or `SCRAPER_STEALTH=0` to switch the last two off and let blocks stand.

The last two tiers are evasion, not politeness — they exist to get past bot detection that sites deliberately deployed. They only ever run after a refusal, never on a site that served the page normally.

**Adaptive backoff.** Requests to the same domain are spaced by `SCRAPER_DELAY_MS`, measured start to start, so the delay caps the request *rate* rather than adding to slow responses. When a domain pushes back — a 429, a 403, a Cloudflare challenge — the delay for that domain doubles, up to 60 seconds, and decays back down once requests succeed again. Domains are tracked independently, so scraping two sites at once costs nothing extra.

**`robots.txt` is not enforced.** It is read only to locate sitemaps; its `Disallow` rules are not consulted and there is no setting to change that. The adaptive per-domain delay is this tool's politeness mechanism.

**A short cache.** Fetched pages are reused for five minutes, so running `fetch_pages` and then `extract_emails` over the same URLs does not fetch everything twice.

## HTTP mode

The default is stdio, which is what `claude mcp add` above uses. To run a persistent shared instance instead:

```bash
SCRAPER_AUTH_TOKEN=$(openssl rand -hex 16) cute-web-scraper --http --port 8080
```

```bash
claude mcp add --transport http cute-web-scraper http://127.0.0.1:8080/mcp
```

It binds `127.0.0.1` and exposes `/mcp` plus a `/health` endpoint. Binding anywhere beyond loopback requires `SCRAPER_AUTH_TOKEN`, and the server refuses to start without it rather than quietly publishing an open scraper to your network.

## Limitations

- No proxy rotation and no CAPTCHA solving. A site that survives all four tiers is reported as blocked rather than guessed at.
- LinkedIn and similar may need `SCRAPER_CHROME_USER_DATA_DIR` pointed at a logged-in Chrome profile.
- `SCRAPER_DELAY_MS=0` removes the polite delay, but backoff still engages when a site pushes back.
- Phone extraction is deliberately conservative: it requires a country code or a trunk prefix, so it misses some bare local formats rather than returning years and order numbers.

## Development

```bash
uv sync --extra dev
```

```bash
uv run pytest -v
```

```bash
uv run pytest -m integration -v -s
```

```bash
uv run ruff check src/ tests/ && uv run mypy src/cute_web_scraper/
```

Unit tests are hermetic and never touch the network. Integration tests hit live sites and are excluded from the default run.

## License

MIT — see [LICENSE](LICENSE).
