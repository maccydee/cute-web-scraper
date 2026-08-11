"""Command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from . import __version__

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from .config import Config

log = logging.getLogger("cute_web_scraper")


def _configure_logging() -> None:
    # stderr only. On stdio transport stdout carries JSON-RPC frames.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cute-web-scraper",
        description="An MCP server that gives Claude web scraping powers",
    )
    parser.add_argument("--version", action="version", version=f"cute-web-scraper {__version__}")
    parser.add_argument("--http", action="store_true", help="Serve over HTTP instead of stdio")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port (default: 8080)")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help=(
            "HTTP bind address (default: 127.0.0.1). Binding beyond loopback requires "
            "SCRAPER_AUTH_TOKEN to be set."
        ),
    )
    return parser


@asynccontextmanager
async def running_server() -> AsyncIterator[tuple[MCPServer, Config]]:
    """Open a Scraper, wire it into a server, and guarantee cleanup."""
    from .config import Config
    from .scraper import Scraper
    from .server import ScraperHolder, create_server
    from .store import ResultStore

    config = Config.from_env()
    store = ResultStore(config.db_path)
    holder = ScraperHolder()
    mcp = create_server(holder)
    log.info("result tables at %s", store.path)
    async with Scraper(config) as scraper:
        holder.set(scraper, store=store, config=config)
        yield mcp, config


async def _run_stdio() -> None:
    log.info("cute-web-scraper v%s starting (transport=stdio)", __version__)
    async with running_server() as (mcp, _config):
        try:
            await mcp.run_stdio_async()
        finally:
            log.info("cute-web-scraper shutting down")


def main() -> None:
    _configure_logging()
    args = _build_parser().parse_args()
    try:
        if args.http:
            from ._http import run_http

            run_http(host=args.host, port=args.port)
        else:
            asyncio.run(_run_stdio())
    except ValueError as exc:
        # Config errors should be a clean exit with a readable message, not a
        # traceback the user has to decode.
        log.error("%s", exc)
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        log.info("Interrupted")


if __name__ == "__main__":
    main()
