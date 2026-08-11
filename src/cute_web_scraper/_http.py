"""Streamable-HTTP transport with optional bearer-token auth."""

from __future__ import annotations

import ipaddress
import logging
import secrets
from collections.abc import Awaitable, Callable

import uvicorn
from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from . import __version__
from .config import Config

log = logging.getLogger(__name__)

_BEARER = "bearer "


class BearerAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, token: str) -> None:
        super().__init__(app)
        self._token = token

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # [RFC 9110 5.1] header names are case-insensitive; the scheme token is too.
        header = request.headers.get("authorization", "")
        if not header.lower().startswith(_BEARER):
            return _unauthorized()
        presented = header[len(_BEARER) :].strip()
        # Constant-time compare so the token cannot be recovered byte by byte.
        if not secrets.compare_digest(presented, self._token):
            return _unauthorized()
        return await call_next(request)


def _unauthorized() -> Response:
    log.warning("401 Unauthorized: missing or invalid bearer token")
    return JSONResponse({"error": "Unauthorized"}, status_code=401)


def check_bind_is_safe(host: str, config: Config) -> None:
    """Refuse to publish an unauthenticated scraper beyond loopback."""
    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = host == "localhost"
    if not is_loopback and not config.auth_token:
        raise ValueError(
            f"Refusing to bind {host} without authentication. "
            "Set SCRAPER_AUTH_TOKEN, or bind 127.0.0.1 (the default)."
        )


async def _health(_request: Request) -> Response:
    return JSONResponse({"status": "ok", "version": __version__})


def build_app(config: Config, mcp: MCPServer) -> Starlette:
    # MCPServer.custom_route is untyped in the SDK, so apply it as a plain call
    # rather than a decorator; that keeps _health's own signature checked.
    mcp.custom_route("/health", methods=["GET"])(_health)

    app = mcp.streamable_http_app()
    if config.auth_token:
        app.add_middleware(BearerAuthMiddleware, token=config.auth_token)
    return app


def run_http(*, host: str, port: int) -> None:
    import asyncio

    from .__main__ import running_server

    async def _main() -> None:
        async with running_server() as (mcp, config):
            check_bind_is_safe(host, config)
            app = build_app(config, mcp)
            auth_state = "bearer token" if config.auth_token else "no auth (loopback only)"
            log.info(
                "cute-web-scraper v%s starting (transport=http host=%s port=%d, %s)",
                __version__,
                host,
                port,
                auth_state,
            )
            server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
            await server.serve()
            log.info("cute-web-scraper shutting down")

    asyncio.run(_main())
