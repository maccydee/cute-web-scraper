# cute-web-scraper

An MCP server that gives Claude web scraping powers.

Ask Claude to scrape a site in plain English. It fetches the pages, renders the JavaScript when needed, and hands back clean markdown or structured data — no selectors, no glue code.

- **Whole sites, not single pages.** Discover every URL from a sitemap, then fetch them in parallel.
- **Contacts and links.** Pull emails, phone numbers, hyperlinks and social profiles from a list of URLs.
- **Markdown, not HTML.** Pages come back as clean markdown, which is far cheaper on context than raw HTML.

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

| Tool | What it does |
|---|---|
| `fetch_page` | One URL to clean markdown, with title, status and link count |
| `fetch_pages` | Many URLs in parallel, returning results and per-URL errors |
| `crawl_site` | Discover a site's pages via sitemap, falling back to link-following |
| `analyze_website` | Detect the platform, find the sitemap, report whether JS is needed |
| `extract_emails` | Email addresses across a list of URLs, with surrounding context |
| `extract_phones` | Phone numbers across a list of URLs, with surrounding context |
| `extract_links` | Every hyperlink, resolved to absolute URLs |
| `extract_social_links` | Social profiles across eight platforms |

A typical run composes them: `analyze_website` → `crawl_site` → `fetch_pages`.

## Example prompts

```
Scrape every product from https://example-shop.com and give me a CSV of name and price.

Find all email addresses on https://company.com and its contact pages.

What platform is https://myblog.com on? Does it need JavaScript to scrape?

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

## How it behaves

**Static by default, browser on request.** Most pages are fetched with a plain HTTP client, which is fast. Pass `js_render: true` for single-page apps, infinite-scroll listings and most modern storefronts, and the page is rendered in real Chromium instead.

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

- No proxy rotation and no CAPTCHA solving. Sites with serious bot defences will block it, and it will back off rather than fight.
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
