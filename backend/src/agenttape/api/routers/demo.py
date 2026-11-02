"""Demo data: seed, start a live Demo Agent run, and reset demo-only records."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from agenttape.api.deps import Actor, Ctx
from agenttape.application.schemas.runs import RunCreateResult
from agenttape.application.services.demo import DemoService
from agenttape.domain.enums import RunSource
from agenttape.domain.errors import ValidationError
from agenttape.seeds.core_demo import DEMO_PROFILES

router = APIRouter(tags=["demo"])


class SeedResult(BaseModel):
    created: dict[str, list[str]]
    existing: dict[str, list[str]]


@router.post("/demo/seed", response_model=SeedResult, summary="Create demo records (idempotent)")
def seed(ctx: Ctx) -> SeedResult:
    # Queue demo runs for the worker instead of executing them inside this request.
    report = DemoService(ctx).seed(execute_inline=False)
    return SeedResult(created=report.created, existing=report.existing)


class DemoRunRequest(BaseModel):
    scenario: str = Field(
        default="success", description="success | fail_then_retry | needs_approval | timeout | fail"
    )
    inputs: dict[str, Any] = Field(default_factory=dict)


class DemoScenario(BaseModel):
    scenario: str
    profile_name: str
    description: str


@router.get("/demo/scenarios", response_model=list[DemoScenario])
def scenarios() -> list[DemoScenario]:
    return [
        DemoScenario(scenario=key, profile_name=name, description=desc)
        for key, (name, desc, _cfg) in DEMO_PROFILES.items()
    ]


@router.post(
    "/demo/runs",
    response_model=RunCreateResult,
    status_code=201,
    summary="Start a live Demo Agent run",
)
def start_demo_run(body: DemoRunRequest, ctx: Ctx, actor: Actor) -> RunCreateResult:
    return DemoService(ctx).start_run(
        scenario=body.scenario, source=RunSource.UI, inputs=body.inputs, actor=actor
    )


class ResetRequest(BaseModel):
    confirm: str = Field(description="Must be the literal string 'reset-demo'")


@router.post("/demo/reset", summary="Delete demo-tagged records only")
def reset_demo(body: ResetRequest, ctx: Ctx, actor: Actor) -> dict[str, dict[str, int]]:
    if body.confirm != "reset-demo":
        raise ValidationError("Confirmation text must be 'reset-demo'")
    return {"deleted": DemoService(ctx).reset(actor=actor)}
