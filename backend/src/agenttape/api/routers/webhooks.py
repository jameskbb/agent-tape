"""Inbound webhooks: ``POST /api/v1/webhooks/{trigger_id}``.

These paths are public (listed in ``PUBLIC_PATHS``): they skip the local API token
because each trigger authenticates with its own hashed token. They enforce their own
body cap (the global cap does not apply to public paths), content type, and rate limits.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from agenttape.api.capture_guard import read_body
from agenttape.api.deps import Ctx
from agenttape.application.schemas.schedules import DeliveryResult
from agenttape.application.services.triggers import MAX_WEBHOOK_BODY_BYTES, TriggerService
from agenttape.triggers.base import InboundRequest

router = APIRouter(tags=["webhooks"])

PUBLIC_PATHS = ["/webhooks/"]


@router.post(
    "/webhooks/{trigger_id}",
    response_model=DeliveryResult,
    status_code=202,
    summary="Deliver a webhook (authenticated with the trigger's own token)",
    responses={
        200: {"model": DeliveryResult, "description": "Duplicate delivery: existing run"},
        401: {"description": "Missing or wrong trigger token"},
        413: {"description": "Payload too large"},
        415: {"description": "Content-Type is not JSON"},
        429: {"description": "Rate limited"},
    },
)
async def deliver(trigger_id: str, request: Request, ctx: Ctx) -> JSONResponse:
    body = await read_body(request, MAX_WEBHOOK_BODY_BYTES)
    if isinstance(body, JSONResponse):
        return body
    inbound = InboundRequest(
        headers={k.lower(): v for k, v in request.headers.items()},
        query=dict(request.query_params),
        body=body,
        client=request.client.host if request.client else "unknown",
        content_type=request.headers.get("content-type", ""),
    )
    outcome = await asyncio.to_thread(TriggerService(ctx).handle_delivery, trigger_id, inbound)
    if outcome.http_status >= 400:
        headers = {"Retry-After": "60"} if outcome.http_status == 429 else None
        return JSONResponse(
            status_code=outcome.http_status,
            content={
                "error": {
                    "code": outcome.result.code or outcome.result.status,
                    "message": outcome.result.message or "",
                    "details": {"delivery_id": outcome.result.delivery_id},
                }
            },
            headers=headers,
        )
    return JSONResponse(
        status_code=outcome.http_status, content=outcome.result.model_dump(mode="json")
    )
