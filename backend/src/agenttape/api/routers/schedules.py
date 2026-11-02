"""Schedules: CRUD, pause/resume, run-now, next-run preview, history, scheduler status."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Response

from agenttape.api.deps import Actor, Ctx, Paging
from agenttape.application.schemas.common import Page
from agenttape.application.schemas.schedules import (
    OccurrenceOut,
    RunNowRequest,
    RunNowResult,
    ScheduleCreate,
    ScheduleOut,
    SchedulePreview,
    SchedulePreviewRequest,
    SchedulerStatusOut,
    ScheduleTargets,
    ScheduleUpdate,
)
from agenttape.application.services.schedules import ScheduleService
from agenttape.scheduler.calc import timezone_names

router = APIRouter(tags=["schedules"])


@router.get("/schedules", response_model=Page[ScheduleOut], summary="List schedules")
def list_schedules(
    ctx: Ctx,
    paging: Paging,
    q: Annotated[str | None, Query(max_length=200)] = None,
    enabled: bool | None = None,
    project_id: str | None = None,
    sort: str = "name",
    order: Annotated[str, Query(pattern="^(asc|desc)$")] = "asc",
) -> Page[ScheduleOut]:
    return ScheduleService(ctx).list_schedules(
        search=q,
        enabled=enabled,
        project_id=project_id,
        sort=sort,
        order=order,
        limit=paging.limit,
        offset=paging.offset,
    )


@router.post("/schedules", response_model=ScheduleOut, status_code=201)
def create_schedule(body: ScheduleCreate, ctx: Ctx, actor: Actor) -> ScheduleOut:
    return ScheduleService(ctx).create(body, actor=actor)


@router.post(
    "/schedules/preview",
    response_model=SchedulePreview,
    summary="Next run times for a draft schedule (DST-aware, in its time zone)",
)
def preview_schedule(body: SchedulePreviewRequest, ctx: Ctx) -> SchedulePreview:
    return ScheduleService(ctx).preview(body)


@router.get("/schedules/timezones", response_model=list[str], summary="IANA time zone names")
def list_timezones() -> list[str]:
    return timezone_names()


@router.get(
    "/schedules/targets",
    response_model=ScheduleTargets,
    summary="Prompts, agent profiles, projects, and workflows a schedule or trigger can use",
)
def schedule_targets(ctx: Ctx) -> ScheduleTargets:
    return ScheduleService(ctx).targets()


@router.get(
    "/scheduler/status", response_model=SchedulerStatusOut, summary="Scheduler owner and health"
)
def scheduler_status(ctx: Ctx) -> SchedulerStatusOut:
    return ScheduleService(ctx).status()


@router.get("/schedules/{schedule_id}", response_model=ScheduleOut)
def get_schedule(schedule_id: str, ctx: Ctx) -> ScheduleOut:
    return ScheduleService(ctx).get(schedule_id)


@router.patch("/schedules/{schedule_id}", response_model=ScheduleOut)
def update_schedule(schedule_id: str, body: ScheduleUpdate, ctx: Ctx, actor: Actor) -> ScheduleOut:
    return ScheduleService(ctx).update(schedule_id, body, actor=actor)


@router.delete("/schedules/{schedule_id}", status_code=204)
def delete_schedule(schedule_id: str, ctx: Ctx, actor: Actor) -> Response:
    ScheduleService(ctx).delete(schedule_id, actor=actor)
    return Response(status_code=204)


@router.post("/schedules/{schedule_id}/pause", response_model=ScheduleOut)
def pause_schedule(schedule_id: str, ctx: Ctx, actor: Actor) -> ScheduleOut:
    return ScheduleService(ctx).set_enabled(schedule_id, False, actor=actor)


@router.post("/schedules/{schedule_id}/resume", response_model=ScheduleOut)
def resume_schedule(schedule_id: str, ctx: Ctx, actor: Actor) -> ScheduleOut:
    return ScheduleService(ctx).set_enabled(schedule_id, True, actor=actor)


@router.post(
    "/schedules/{schedule_id}/run-now",
    response_model=RunNowResult,
    summary="Create an occurrence now (respects the concurrency policy unless force)",
)
def run_schedule_now(
    schedule_id: str, ctx: Ctx, actor: Actor, body: RunNowRequest | None = None
) -> RunNowResult:
    return ScheduleService(ctx).run_now(schedule_id, force=bool(body and body.force), actor=actor)


@router.get(
    "/schedules/{schedule_id}/history",
    response_model=Page[OccurrenceOut],
    summary="Occurrences: created, queued, skipped, missed, replaced, failed",
)
def schedule_history(schedule_id: str, ctx: Ctx, paging: Paging) -> Page[OccurrenceOut]:
    return ScheduleService(ctx).history(schedule_id, limit=paging.limit, offset=paging.offset)
