"""Inbound MCP capture: ``POST /api/v1/mcp/findings``.

This path is public (listed in ``PUBLIC_PATHS``): it skips the local API token because it
authenticates with its own capture token instead - a per-attempt token (``X-AgentTape-Attempt``
present) for a run AgentTape launched, or the per-machine ``mcp`` token (no attempt header) for
an interactive or Herdr-hosted session, resolved to a run through
``ObservedSessionService.resolve_for_mcp``. It enforces loopback-only access, its own body cap,
content type, and authentication before any database access - mirroring
``api/routers/webhooks.py``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from agenttape.api.capture_guard import error_response, read_json_body, require_json_content_type
from agenttape.api.deps import Ctx
from agenttape.application.schemas.capture import FindingRecordIn, FindingRecordOut
from agenttape.application.services.capture import CaptureService, finding_payload
from agenttape.application.services.hook_ingest import hook_ingest
from agenttape.capture.claude_hooks import is_absolute_path
from agenttape.security.capture import (
    CaptureTokenStore,
    bearer_token,
    capture_rejection,
    verify_attempt_token,
)
from agenttape.security.ratelimit import RateLimiter

router = APIRouter(tags=["mcp"])

PUBLIC_PATHS = ["/mcp/"]
MAX_MCP_BODY_BYTES = 256 * 1024
FINDINGS_PATH = "/mcp/findings"

#: Checked only on an authentication failure, keyed by client host (mirrors GuardMiddleware's
#: own auth-failure limiter).
_AUTH_FAILURES = RateLimiter(rate=20, per_seconds=60)
#: Checked after authentication succeeds, keyed by attempt id.
_WRITES = RateLimiter(rate=120, per_seconds=60, burst=60)

#: How long an interactive finding may wait in the hook ingest queue before it starts. Past
#: this it is cancelled (nothing is written) and the caller is told so. Well under the stdio
#: MCP server's own HTTP timeout (``agenttape.mcp.client._TIMEOUT_SECONDS``), so the agent
#: always hears AgentTape's answer, never a client timeout.
MCP_QUEUE_WAIT_SECONDS = 5.0
#: How much longer a finding that already started being written is waited for: its own
#: ``BEGIN IMMEDIATE`` can wait out one SQLite busy timeout (10 s). Past this the caller is
#: told it may have been recorded; recording the identical finding again within a minute
#: is safe (``capture.FINDING_RETRY_WINDOW_SECONDS``: AgentTape keeps one copy).
MCP_WRITE_WAIT_SECONDS = 15.0


def _json_size(value: Any) -> int:
    return len(json.dumps(value, default=str, ensure_ascii=False).encode("utf-8"))


@router.post(
    FINDINGS_PATH,
    response_model=FindingRecordOut,
    status_code=201,
    summary="Record a finding from the AgentTape MCP server (loopback callers only; "
    "per-attempt capture token, or the per-machine mcp token with cwd and instance_id)",
    responses={
        401: {"description": "Missing or invalid capture token (per-attempt or mcp)"},
        403: {"description": "Not a loopback caller, or a browser/proxied request"},
        413: {"description": "Payload too large"},
        415: {"description": "Content-Type is not JSON"},
        409: {
            "description": (
                "Ambiguous session, a session_id this caller cannot use, or observation "
                "was cancelled"
            )
        },
        421: {"description": "Untrusted Host header"},
        422: {"description": "Finding validation failed, or outside every registered project"},
        429: {"description": "Rate limited"},
        503: {
            "description": "AgentTape's hook ingest thread is busy: the finding was not "
            "recorded (ingest_busy), or may have been (finding_write_pending); recording "
            "the identical finding again within a minute is safe"
        },
    },
)
async def record_finding(request: Request, ctx: Ctx) -> JSONResponse:
    client_host = request.client.host if request.client else None
    headers = {k.lower(): v for k, v in request.headers.items()}
    rejection = capture_rejection(client_host, headers)
    if rejection is not None:
        return error_response(rejection.status, rejection.code, rejection.message)

    unsupported = require_json_content_type(headers)
    if unsupported is not None:
        return unsupported

    attempt_id = headers.get("x-agenttape-attempt")
    token = bearer_token(headers)

    def auth_failed() -> JSONResponse:
        key = client_host or "unknown"
        if not _AUTH_FAILURES.allow(key):
            return error_response(
                429,
                "rate_limited",
                "Too many failed capture authentication attempts",
                retry_after=_AUTH_FAILURES.retry_after(key),
            )
        return error_response(
            401, "invalid_capture_token", "Invalid or missing capture credentials"
        )

    # Authenticate before touching the body at all (mirrors claude_code_hook): a caller
    # with no or the wrong credential pays only for a header check, never a body read or
    # JSON/Pydantic parse.
    if attempt_id:
        if not token or not verify_attempt_token(ctx.settings.data_dir, attempt_id, token):
            return auth_failed()
    else:
        if not token or not CaptureTokenStore(ctx.settings.data_dir).verify("mcp", token):
            return auth_failed()

    data = await read_json_body(request, MAX_MCP_BODY_BYTES)
    if isinstance(data, JSONResponse):
        return data

    try:
        record = FindingRecordIn.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[:3]
        message = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in first)
        return error_response(422, "invalid_finding", message or "Finding validation failed")

    if _json_size(finding_payload(record)) > ctx.settings.max_event_payload_bytes:
        return error_response(
            413,
            "finding_too_large",
            "This finding is too large to record; split it into smaller findings or quote "
            "fewer lines per citation.",
        )

    if attempt_id:
        verified = await asyncio.to_thread(_verify_attempt, ctx, attempt_id)
        if verified is None:
            return auth_failed()
        run_id, attempt_id = verified

        if not _WRITES.allow(attempt_id):
            return error_response(
                429,
                "rate_limited",
                "Too many findings recorded for this attempt; slow down",
                retry_after=_WRITES.retry_after(attempt_id),
            )

        result = await asyncio.to_thread(
            CaptureService(ctx).record_finding, run_id=run_id, attempt_id=attempt_id, record=record
        )
        return JSONResponse(status_code=201, content=result.model_dump(mode="json"))

    # Interactive / Herdr-hosted path: no per-attempt token - already authenticated above
    # against the per-machine "mcp" token (an attempt or hooks token is refused there just
    # as a wrong token is).
    if not record.cwd or not is_absolute_path(record.cwd):
        return error_response(422, "invalid_finding", "cwd must be an absolute path")
    if not record.instance_id:
        return error_response(422, "invalid_finding", "instance_id is required")

    # Keyed on the (loopback) client, not the caller-supplied `instance_id`: a caller
    # holding the shared per-machine `mcp` token can mint a fresh `instance_id` on every
    # call, which would otherwise both dodge this limiter and defeat resolve_for_mcp's
    # "already bound" fast path, minting a brand-new `observed` run per call.
    write_key = f"mcp:{client_host or 'unknown'}"
    if not _WRITES.allow(write_key):
        return error_response(
            429,
            "rate_limited",
            "Too many findings recorded for this session; slow down",
            retry_after=_WRITES.retry_after(write_key),
        )

    # On the hook ingest thread, after every hook already received: a read whose hooks are
    # still queued happened before this finding and must be sequenced before it too.
    service = CaptureService(ctx)
    job = hook_ingest(ctx).in_order(lambda: service.record_interactive_finding(record))
    try:
        outcome = await asyncio.wait_for(
            asyncio.shield(asyncio.wrap_future(job)), timeout=MCP_QUEUE_WAIT_SECONDS
        )
    except TimeoutError:
        if job.cancel():  # still queued: it will never run, so nothing was written
            return error_response(
                503,
                "ingest_busy",
                "AgentTape is busy recording hooks, so this finding was not recorded. "
                "Record it again in a moment.",
                retry_after=1,
            )
        try:
            outcome = await asyncio.wait_for(
                asyncio.shield(asyncio.wrap_future(job)), timeout=MCP_WRITE_WAIT_SECONDS
            )
        except TimeoutError:
            return error_response(
                503,
                "finding_write_pending",
                "AgentTape is still writing this finding and could not confirm it in time, "
                "so it may have been recorded. Recording the identical finding again within "
                "a minute is safe: AgentTape keeps one copy.",
                retry_after=1,
            )
    if outcome.status == "recorded":
        assert outcome.result is not None
        return JSONResponse(status_code=201, content=outcome.result.model_dump(mode="json"))
    if outcome.status == "ambiguous":
        return error_response(409, "ambiguous_session", outcome.message or "Ambiguous session")
    if outcome.status == "outside_projects":
        return error_response(
            422, "outside_projects", outcome.message or "Outside every registered project"
        )
    if outcome.status == "session_mismatch":
        return error_response(409, "session_mismatch", outcome.message or "Session mismatch")
    return error_response(409, "observation_cancelled", outcome.message or "Observation cancelled")


def _verify_attempt(ctx: Ctx, attempt_id: str) -> tuple[str, str] | None:
    with ctx.session() as session:
        return CaptureService.verify_attempt(session, attempt_id)
