"""Shared guards for the public capture and webhook endpoints.

The hooks, MCP, and webhook routers skip the local API token (each is listed in its own
``PUBLIC_PATHS``), so the global body cap does not apply to them and each enforces its own
limits. The pieces they have in common live here so a guard fix lands once: the error
envelope, the JSON content-type check, and the capped body readers.

This module sits beside ``api/routers/``, not inside it, so ``discover_routers`` never
imports it as a router.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


def error_response(
    status: int, code: str, message: str, *, retry_after: float | None = None
) -> JSONResponse:
    """The API's error envelope. ``retry_after`` (seconds, from a limiter) becomes a
    ``Retry-After`` header rounded up to a whole second, never less than 1."""
    headers = (
        {"Retry-After": str(max(1, int(retry_after) + 1))} if retry_after is not None else None
    )
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "details": {}}},
        headers=headers,
    )


def require_json_content_type(headers: Mapping[str, str]) -> JSONResponse | None:
    """A 415 unless ``headers`` (lower-cased names) declare ``application/json``."""
    content_type = headers.get("content-type", "")
    if content_type.split(";")[0].strip() != "application/json":
        return error_response(
            415, "unsupported_media_type", "Content-Type must be application/json"
        )
    return None


async def read_body(request: Request, cap: int) -> bytes | JSONResponse:
    """The request body, or a 413 once it exceeds ``cap`` bytes. A declared
    Content-Length over the cap is refused before reading; a chunked or understated body
    is refused as soon as the streamed total passes the cap."""
    too_large = f"Payload exceeds {cap} bytes"
    length = request.headers.get("content-length")
    if length is not None and length.isdigit() and int(length) > cap:
        return error_response(413, "payload_too_large", too_large)
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > cap:
            return error_response(413, "payload_too_large", too_large)
        chunks.append(chunk)
    return b"".join(chunks)


async def read_json_body(request: Request, cap: int) -> Any | JSONResponse:
    """The request body parsed as JSON (an empty body is ``{}``), or the 413 from
    :func:`read_body`, or a 400 when it is not valid UTF-8 JSON. Callers check
    ``isinstance(result, JSONResponse)`` and return it."""
    body = await read_body(request, cap)
    if isinstance(body, JSONResponse):
        return body
    try:
        return json.loads(body.decode("utf-8")) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return error_response(400, "invalid_json", "Request body is not valid JSON")
