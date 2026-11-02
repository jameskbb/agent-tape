"""Inbound Claude Code ``http`` hooks: ``POST /api/v1/hooks/claude-code``.

This path is public (listed in ``PUBLIC_PATHS``): it skips the local API token because it
authenticates with its own per-machine ``hooks`` capture token. It enforces loopback-only
access, its own body cap, content type, and token verification before any database access -
mirroring ``api/routers/webhooks.py`` and ``api/routers/mcp.py``.

A 2xx response is always exactly ``{}``: this handler never returns a field a hook
decision could read (hooks are strictly observational and never gate a tool call).

The handler itself never touches the database: every check up to and including
``ingest.submit`` runs on the request thread and is either header-only, a bounded body
read, or pure parsing. The actual ``ObservedSessionService.ingest_hook`` call runs on the
one hook ingest thread per app context
(:class:`agenttape.application.services.hook_ingest.HookIngest`, which also writes every
interactive MCP finding in order with the hooks), *after* the ``{}`` response has already
been decided, so a locked or slow database can never make a Claude Code tool call wait.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.requests import ClientDisconnect

from agenttape.api.capture_guard import error_response, read_json_body, require_json_content_type
from agenttape.api.deps import Ctx
from agenttape.application.services.hook_ingest import hook_ingest
from agenttape.capture.claude_hooks import HookPayloadError, parse_hook, session_hash
from agenttape.security.capture import CaptureTokenStore, bearer_token, capture_rejection
from agenttape.security.ratelimit import RateLimiter

__all__ = ["HOOK_URL_PATH", "PUBLIC_PATHS", "hook_ingest", "router"]

router = APIRouter(tags=["hooks"])

PUBLIC_PATHS = ["/hooks/"]
HOOK_URL_PATH = "/api/v1/hooks/claude-code"
MAX_HOOK_BODY_BYTES = 1024 * 1024

#: Repeated failed tokens from one client (401) past this rate get 429 instead - a
#: brute-force guard that never touches the database (client host only).
_AUTH_FAILURE_LIMITER = RateLimiter(rate=20, per_seconds=60)
#: Hooks from one session past this rate get 429 (and are counted as dropped) - the flood
#: guard for a single runaway session.
_SESSION_LIMITER = RateLimiter(rate=1200, per_seconds=60, burst=300)


@router.post(
    "/hooks/claude-code",
    status_code=200,
    summary="Receive a Claude Code http hook (observational only; always answers {})",
    responses={
        200: {"description": "Always {} - AgentTape's hook handler never gates a tool call"},
        400: {
            "description": "Body is not valid JSON, or the client disconnected before it was read"
        },
        401: {"description": "Missing or invalid hooks capture token"},
        403: {"description": "Not a loopback caller, or a browser/proxied request"},
        413: {"description": "Payload too large"},
        415: {"description": "Content-Type is not JSON"},
        421: {"description": "Untrusted Host header"},
        422: {"description": "Hook body is not shaped like a Claude Code hook payload"},
        429: {
            "description": "Too many failed tokens, or too many hooks for one session "
            "(with Retry-After)"
        },
        503: {"description": "AgentTape's ingest queue is saturated; try again shortly"},
    },
)
async def claude_code_hook(request: Request, ctx: Ctx) -> JSONResponse:
    client_host = request.client.host if request.client else None
    headers = {k.lower(): v for k, v in request.headers.items()}
    rejection = capture_rejection(client_host, headers)
    if rejection is not None:
        return error_response(rejection.status, rejection.code, rejection.message)

    unsupported = require_json_content_type(headers)
    if unsupported is not None:
        return unsupported

    token = bearer_token(headers)
    # Checked here on the event loop (a small file read and a constant-time compare, as
    # the MCP router does), never on the shared thread pool: work stuck there on a locked
    # database must not hold a hook past Claude Code's 3s timeout before its body is read.
    valid_token = bool(token) and CaptureTokenStore(ctx.settings.data_dir).verify("hooks", token)
    if not valid_token:
        auth_key = client_host or "unknown"
        if not _AUTH_FAILURE_LIMITER.allow(auth_key):
            return error_response(
                429,
                "too_many_failed_tokens",
                "Too many failed hook tokens; slow down",
                retry_after=_AUTH_FAILURE_LIMITER.retry_after(auth_key),
            )
        return error_response(
            401, "invalid_capture_token", "Invalid or missing hooks capture token"
        )

    # From here on the caller holds the hooks token, so a hook this handler cannot record
    # is a real Claude Code hook lost: count it as dropped, for its session when the body
    # names one, otherwise for every session AgentTape is observing (it could be any).
    ingest = hook_ingest(ctx)
    try:
        data = await read_json_body(request, MAX_HOOK_BODY_BYTES)
    except ClientDisconnect:
        # Claude Code gave up (its 3s timeout) and closed the connection before the body
        # was read, so it ran the tool call anyway and nothing names the session.
        ingest.drops.mark_unattributed(at=ctx.clock.now())
        return error_response(400, "client_disconnected", "The hook request was abandoned")
    if isinstance(data, JSONResponse):
        ingest.drops.mark_unattributed(at=ctx.clock.now())  # 413 over the cap, 400 not JSON
        return data

    claimed_run_id = headers.get("x-agenttape-run-id")
    try:
        hook = parse_hook(data, claimed_run_id=claimed_run_id)
    except HookPayloadError as exc:
        named = data.get("session_id") if isinstance(data, dict) else None
        if isinstance(named, str) and named:
            ingest.mark_dropped(session_hash(named))
        else:
            ingest.drops.mark_unattributed(at=ctx.clock.now())
        return error_response(422, "invalid_hook_payload", str(exc))
    if hook is None:
        return JSONResponse(status_code=200, content={})

    hashed = session_hash(hook.session_id)
    if not _SESSION_LIMITER.allow(hashed):
        ingest.mark_dropped(hashed)
        return error_response(
            429,
            "too_many_hooks",
            "Too many hooks for this session; slow down",
            retry_after=_SESSION_LIMITER.retry_after(hashed),
        )

    if not ingest.submit(hook):
        ingest.mark_dropped(hashed)
        return error_response(503, "ingest_saturated", "AgentTape's hook ingest queue is full")

    return JSONResponse(status_code=200, content={})
