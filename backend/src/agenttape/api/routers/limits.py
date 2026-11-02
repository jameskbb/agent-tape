"""Limits: global concurrency and default budget settings, and a run's effective limits."""

from __future__ import annotations

from fastapi import APIRouter

from agenttape.api.deps import Actor, Ctx
from agenttape.application.schemas.schedules import LimitsOut, LimitsSettings, RunLimitsOut
from agenttape.application.services.limits import LimitsService

router = APIRouter(tags=["limits"])


@router.get("/settings/limits", response_model=LimitsOut, summary="Global limits and live usage")
def get_limits(ctx: Ctx) -> LimitsOut:
    return LimitsService(ctx).get()


@router.put("/settings/limits", response_model=LimitsOut, summary="Update global limits")
def update_limits(body: LimitsSettings, ctx: Ctx, actor: Actor) -> LimitsOut:
    return LimitsService(ctx).update(body, actor=actor)


@router.get(
    "/runs/{run_id}/limits",
    response_model=RunLimitsOut,
    summary="Effective budget, budget checks, timeouts, retry policy, and concurrency scopes",
)
def run_limits(run_id: str, ctx: Ctx) -> RunLimitsOut:
    return LimitsService(ctx).for_run(run_id)
