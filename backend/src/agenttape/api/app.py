"""FastAPI application factory.

* Every module in ``agenttape/api/routers/`` that exposes ``router`` (an ``APIRouter``) is
  mounted under ``/api/v1`` automatically. A module may also define
  ``PUBLIC_PATHS: list[str]`` - path prefixes (relative to ``/api/v1``) that skip local
  API-token auth because they authenticate differently (e.g. webhooks with their own
  hashed tokens). Health endpoints are always public.
* The built frontend (``agenttape/web_dist``) is served at ``/`` with SPA fallback.
* Errors use the envelope ``{"error": {"code", "message", "details"}}``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agenttape import __version__
from agenttape.api.errors import install_error_handlers
from agenttape.application.context import AppContext
from agenttape.domain.errors import PolicyViolationError
from agenttape.plugins import import_submodules
from agenttape.runtime.base import Supervisor
from agenttape.security.paths import safe_join
from agenttape.security.ratelimit import RateLimiter
from agenttape.security.tokens import verify_token

log = logging.getLogger("agenttape.api")

API_PREFIX = "/api/v1"
ROUTERS_PACKAGE = "agenttape.api.routers"
WEB_DIST = Path(__file__).resolve().parent.parent / "web_dist"
ALWAYS_PUBLIC = ("/api/v1/health", "/api/v1/ready")


def _json_error(status: int, code: str, message: str) -> bytes:
    return json.dumps({"error": {"code": code, "message": message, "details": {}}}).encode()


UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
#: Sent on every response unless the route already set the header (artifacts set a stricter
#: CSP of their own).
SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
)


def _origin_host(origin: str) -> str | None:
    """The ``host[:port]`` part of an ``Origin`` header, or None for ``null``/garbage."""
    scheme, sep, rest = origin.partition("://")
    if not sep or scheme not in ("http", "https") or not rest:
        return None
    return rest.split("/", 1)[0]


class GuardMiddleware:
    """Request-size cap, local API-token authentication, and browser-origin checks
    (pure ASGI; SSE-safe).

    When no token is required (the loopback default), the API trusts whoever reaches the
    socket, so two browser attacks are refused here: DNS rebinding (a request whose ``Host``
    is not a loopback or configured name) and cross-site writes (an unsafe method whose
    ``Origin`` or ``Sec-Fetch-Site`` shows another site sent it). Public paths such as
    webhooks authenticate with their own tokens and skip both checks.
    """

    def __init__(self, app: ASGIApp, *, ctx: AppContext, public_prefixes: list[str]) -> None:
        self.app = app
        self.ctx = ctx
        self.public = tuple(public_prefixes)
        self.auth_failures = RateLimiter(rate=20, per_seconds=60)

    def _is_public(self, path: str) -> bool:
        # A prefix ending in "/" covers its subtree; any other must match a whole segment,
        # so "/api/v1/health" never exempts a future "/api/v1/health-admin".
        return any(
            path.startswith(p) if p.endswith("/") else path == p or path.startswith(p + "/")
            for p in self.public
        )

    def _browser_rejection(self, method: str, headers: dict[str, str]) -> str | None:
        settings = self.ctx.settings
        if settings.token_required:
            return None  # a cross-site page cannot supply the token
        host = headers.get("host", "")
        if host and not settings.accepts_host(host):
            return "untrusted_host"
        if method in UNSAFE_METHODS:
            if headers.get("sec-fetch-site") == "cross-site":
                return "cross_site_request"
            origin = headers.get("origin")
            if origin is not None:
                origin_host = _origin_host(origin)
                if origin_host is None or not settings.accepts_host(origin_host):
                    return "cross_site_request"
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        send = _with_security_headers(send)
        path: str = scope.get("path", "")
        public = self._is_public(path)
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])
        }
        if not public:
            reason = self._browser_rejection(scope.get("method", "GET"), headers)
            if reason == "untrusted_host":
                await self._reject(
                    send,
                    421,
                    reason,
                    "This host name is not accepted. Open AgentTape at 127.0.0.1 or localhost, "
                    "or add the name to AGENTTAPE_ALLOWED_HOSTS.",
                )
                return
            if reason is not None:
                await self._reject(
                    send, 403, reason, "Cross-site requests cannot change AgentTape data"
                )
                return
        limit = self.ctx.settings.max_request_body_bytes
        length = headers.get("content-length")
        if length is not None and length.isdigit() and int(length) > limit and not public:
            await self._reject(
                send, 413, "payload_too_large", f"Request body exceeds {limit} bytes"
            )
            return
        if self.ctx.settings.token_required and path.startswith("/api/") and not public:
            client = (scope.get("client") or ("unknown", 0))[0]
            token = headers.get("x-agenttape-token")
            auth = headers.get("authorization", "")
            if not token and auth.lower().startswith("bearer "):
                token = auth[7:].strip()
            stored = self.ctx.settings.effective_token_hash()
            if stored is None:
                await self._reject(
                    send,
                    503,
                    "token_not_configured",
                    "API token auth is required but no token is configured",
                )
                return
            if not verify_token(token, stored):
                if not self.auth_failures.allow(str(client)):
                    await self._reject(
                        send, 429, "rate_limited", "Too many failed authentication attempts"
                    )
                    return
                await self._reject(send, 401, "unauthorized", "A valid API token is required")
                return
        if public:
            await self.app(scope, receive, send)
            return
        if length is not None or scope.get("method") not in ("POST", "PUT", "PATCH"):
            await self.app(scope, receive, send)
            return
        # No Content-Length (chunked): buffer up to the cap, reject above it, then replay.
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            body.extend(message.get("body", b""))
            if len(body) > limit:
                await self._reject(
                    send, 413, "payload_too_large", f"Request body exceeds {limit} bytes"
                )
                return
            if not message.get("more_body", False):
                break
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    @staticmethod
    async def _reject(send: Send, status: int, code: str, message: str) -> None:
        body = _json_error(status, code, message)
        start: Message = {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
        await send(start)
        await send({"type": "http.response.body", "body": body})


def _with_security_headers(send: Send) -> Send:
    async def wrapped(message: Message) -> None:
        if message["type"] == "http.response.start":
            present = {k.lower() for k, _ in message.get("headers", [])}
            extra = [(k, v) for k, v in SECURITY_HEADERS if k not in present]
            if extra:
                message = {**message, "headers": [*message.get("headers", []), *extra]}
        await send(message)

    return wrapped


def discover_routers() -> tuple[list[APIRouter], list[str]]:
    routers: list[APIRouter] = []
    public: list[str] = list(ALWAYS_PUBLIC)
    for module in import_submodules(ROUTERS_PACKAGE):
        router = getattr(module, "router", None)
        if isinstance(router, APIRouter):
            routers.append(router)
            public.extend(API_PREFIX + p for p in getattr(module, "PUBLIC_PATHS", []))
    return routers, public


_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)


def spa_csp(index_html: str) -> str:
    """Content-Security-Policy for the web UI: same-origin only, never framed. Inline
    scripts in ``index.html`` (the theme bootstrap) are allowed by hash."""
    hashes = " ".join(
        "'sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode() + "'"
        for body in _INLINE_SCRIPT.findall(index_html)
    )
    return (
        f"default-src 'self'; script-src 'self' {hashes}".rstrip()
        + "; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
        "font-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; "
        "form-action 'self'; frame-ancestors 'none'"
    )


def _install_spa(app: FastAPI, dist: Path) -> None:
    index = dist / "index.html"
    csp: dict[str, str] = {}

    def page_headers() -> dict[str, str]:
        if "value" not in csp:
            csp["value"] = spa_csp(index.read_text(encoding="utf-8"))
        return {"Cache-Control": "no-cache", "Content-Security-Policy": csp["value"]}

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> Response:
        if full_path.startswith("api/") or full_path == "api":
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "not_found",
                        "message": f"No API route /{full_path}",
                        "details": {},
                    }
                },
            )
        if not index.is_file():
            return HTMLResponse(
                "<!doctype html><title>AgentTape</title><body style='font-family:sans-serif;padding:2rem'>"
                "<h1>AgentTape API is running</h1><p>The web UI has not been built. Run "
                "<code>make build</code> (or <code>pnpm --dir frontend build</code>) and restart, "
                "or use <code>make dev</code> for the Vite dev server.</p>"
                "<p>API docs: <a href='/api/docs'>/api/docs</a></p></body>",
                status_code=200,
            )
        if full_path:
            try:
                candidate = safe_join(dist, full_path)
            except PolicyViolationError:
                candidate = None
            if candidate is not None and candidate.is_file():
                cache = (
                    "public, max-age=31536000, immutable"
                    if full_path.startswith("assets/")
                    else "no-cache"
                )
                return FileResponse(candidate, headers={"Cache-Control": cache})
        return FileResponse(index, headers=page_headers())


def create_app(
    ctx: AppContext, *, services: list[str] | None = None, web_dist: Path | None = None
) -> FastAPI:
    """Build the ASGI app. ``services`` names background services to run in-process."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        supervisor: Supervisor | None = None
        if services:
            supervisor = Supervisor(ctx, services)
            await supervisor.start()
        app.state.supervisor = supervisor
        try:
            yield
        finally:
            if supervisor is not None:
                await supervisor.stop()

    app = FastAPI(
        title="AgentTape API",
        version=__version__,
        description=(
            "Local-first control plane and flight recorder for AI coding agents. "
            "All endpoints live under /api/v1; errors use the envelope "
            '{"error": {"code", "message", "details"}}.'
        ),
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.ctx = ctx
    install_error_handlers(app)
    routers, public = discover_routers()
    for router in routers:
        app.include_router(router, prefix=API_PREFIX)
    app.state.public_paths = public
    app.add_middleware(GuardMiddleware, ctx=ctx, public_prefixes=public)
    _install_spa(app, web_dist or WEB_DIST)
    return app


def openapi_schema(ctx: AppContext) -> dict[str, Any]:
    return create_app(ctx).openapi()
