"""Runs: create (idempotent), list/filter, detail, cancel, retry, events, SSE, logs, export."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from agenttape.api.deps import Actor, Ctx, Paging
from agenttape.application.schemas.common import Page
from agenttape.application.schemas.runs import (
    EventOut,
    EventPage,
    RunCancel,
    RunCreate,
    RunCreateResult,
    RunDetail,
    RunOut,
    RunRetry,
)
from agenttape.application.services.exports import ExportService
from agenttape.application.services.runs import RunService
from agenttape.domain.enums import TERMINAL_STATUSES, RunStatus
from agenttape.domain.errors import NotFoundError, PolicyViolationError, ValidationError
from agenttape.infrastructure.db.models import Run
from agenttape.security.paths import safe_join

router = APIRouter(tags=["runs"])

SSE_BATCH = 200
SSE_WAIT_SECONDS = 1.0


@router.post(
    "/runs",
    response_model=RunCreateResult,
    status_code=201,
    summary="Create a run (idempotent with Idempotency-Key)",
    responses={
        200: {"model": RunCreateResult, "description": "Existing run for this idempotency key"}
    },
)
def create_run(
    body: RunCreate,
    ctx: Ctx,
    actor: Actor,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=255)] = None,
) -> RunCreateResult:
    if idempotency_key and not body.idempotency_key:
        body = body.model_copy(update={"idempotency_key": idempotency_key})
    result = RunService(ctx).create_run(body, actor=actor)
    if not result.created:
        response.status_code = 200
    return result


@router.get(
    "/runs", response_model=Page[RunOut], summary="List runs with filters, search, and sorting"
)
def list_runs(
    ctx: Ctx,
    paging: Paging,
    status: Annotated[list[str] | None, Query()] = None,
    source: Annotated[list[str] | None, Query()] = None,
    adapter_type: str | None = None,
    project_id: str | None = None,
    prompt_template_id: str | None = None,
    agent_profile_id: str | None = None,
    workflow_id: str | None = None,
    schedule_id: str | None = None,
    parent_run_id: str | None = None,
    failure_category: str | None = None,
    kind: str | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    is_demo: bool | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    sort: str = "created_at",
    order: Annotated[str, Query(pattern="^(asc|desc)$")] = "desc",
) -> Page[RunOut]:
    return RunService(ctx).list_runs(
        status=status,
        source=source,
        adapter_type=adapter_type,
        project_id=project_id,
        prompt_template_id=prompt_template_id,
        agent_profile_id=agent_profile_id,
        workflow_id=workflow_id,
        schedule_id=schedule_id,
        parent_run_id=parent_run_id,
        failure_category=failure_category,
        kind=kind,
        search=q,
        is_demo=is_demo,
        created_after=created_after,
        created_before=created_before,
        sort=sort,
        order=order,
        limit=paging.limit,
        offset=paging.offset,
    )


@router.get(
    "/runs/{run_id}",
    response_model=RunDetail,
    summary="Run detail with attempts, snapshot, artifacts",
)
def get_run(run_id: str, ctx: Ctx) -> RunDetail:
    return RunService(ctx).get_detail(run_id)


class RunPatch(BaseModel):
    notes: str | None = Field(default=None, max_length=20_000)
    tags: list[str] | None = None


@router.patch("/runs/{run_id}", response_model=RunOut, summary="Update run notes and tags")
def patch_run(run_id: str, body: RunPatch, ctx: Ctx) -> RunOut:
    return RunService(ctx).update_notes(run_id, notes=body.notes, tags=body.tags)


@router.post("/runs/{run_id}/cancel", response_model=RunOut, summary="Cancel a run")
def cancel_run(run_id: str, ctx: Ctx, actor: Actor, body: RunCancel | None = None) -> RunOut:
    return RunService(ctx).cancel_run(run_id, reason=body.reason if body else None, actor=actor)


@router.post(
    "/runs/{run_id}/retry", response_model=RunOut, summary="Retry a failed run (new attempt)"
)
def retry_run(run_id: str, ctx: Ctx, actor: Actor, body: RunRetry | None = None) -> RunOut:
    return RunService(ctx).retry_run(run_id, reason=body.reason if body else None, actor=actor)


@router.get(
    "/runs/{run_id}/events", response_model=EventPage, summary="Paginated, filterable events"
)
def list_events(
    run_id: str,
    ctx: Ctx,
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    type: Annotated[list[str] | None, Query()] = None,
    severity: Annotated[list[str] | None, Query()] = None,
    attempt_id: str | None = None,
    node_key: str | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> EventPage:
    return RunService(ctx).list_events(
        run_id,
        after_sequence=after_sequence,
        limit=limit,
        types=type,
        severities=severity,
        attempt_id=attempt_id,
        node_key=node_key,
        text=q,
    )


def _sse_data(event: EventOut, cap: int) -> str:
    data = event.model_dump_json()
    if len(data.encode("utf-8")) <= cap:
        return data
    stub = event.model_dump(mode="json", exclude={"payload"})
    stub["payload"] = {"truncated_for_stream": True}
    return json.dumps(stub)


@router.get(
    "/runs/{run_id}/events/stream",
    summary="Live events over Server-Sent Events (resume with Last-Event-ID or after_sequence)",
    response_class=Response,
)
async def stream_events(
    run_id: str,
    request: Request,
    ctx: Ctx,
    after_sequence: Annotated[int | None, Query(ge=0)] = None,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> EventSourceResponse:
    service = RunService(ctx)
    await asyncio.to_thread(service.get_run, run_id)  # 404 early
    cursor = after_sequence or 0
    if last_event_id and last_event_id.isdigit():
        cursor = max(cursor, int(last_event_id))
    cap = ctx.settings.max_sse_message_bytes

    def fetch(after: int) -> tuple[list[EventOut], str]:
        with ctx.session() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise NotFoundError("run", run_id)
            rows = service.events_after(session, run_id, after, SSE_BATCH)
            return [EventOut.model_validate(r) for r in rows], run.status

    async def generator() -> Any:
        nonlocal cursor
        while True:
            if await request.is_disconnected():
                return
            events, status = await asyncio.to_thread(fetch, cursor)
            for event in events:
                cursor = event.sequence
                yield {
                    "id": str(event.sequence),
                    "event": "run_event",
                    "data": _sse_data(event, cap),
                }
            if events:
                continue
            if RunStatus(status) in TERMINAL_STATUSES:
                yield {
                    "event": "end",
                    "data": json.dumps({"status": status, "last_sequence": cursor}),
                }
                return
            await ctx.notifier.wait(run_id, timeout=SSE_WAIT_SECONDS)

    return EventSourceResponse(
        generator(), ping=15, headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@router.get(
    "/runs/{run_id}/logs/{attempt_number}/{stream}", summary="Download a raw (redacted) attempt log"
)
def download_log(run_id: str, attempt_number: int, stream: str, ctx: Ctx) -> FileResponse:
    if stream not in ("stdout", "stderr"):
        raise ValidationError("stream must be 'stdout' or 'stderr'")
    RunService(ctx).get_run(run_id)
    base = ctx.settings.logs_dir
    try:
        path = safe_join(base, f"{run_id}/attempt-{int(attempt_number)}.{stream}.log")
    except PolicyViolationError as exc:
        raise NotFoundError("log", f"{run_id}/{attempt_number}/{stream}") from exc
    if not path.is_file():
        raise NotFoundError("log", f"{run_id}/{attempt_number}/{stream}")
    return FileResponse(
        path,
        media_type="text/plain; charset=utf-8",
        filename=f"{run_id}-attempt-{attempt_number}-{stream}.log",
    )


@router.get("/runs/{run_id}/export", summary="Portable, sanitized JSON bundle of a run")
def export_run(run_id: str, ctx: Ctx) -> Response:
    bundle = ExportService(ctx).run_bundle(run_id)
    return Response(
        content=json.dumps(bundle, indent=2, default=str),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="agenttape-run-{run_id}.json"'},
    )
