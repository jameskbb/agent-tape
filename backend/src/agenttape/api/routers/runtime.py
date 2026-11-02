"""Runtime (M2): a run's Herdr references, Herdr status, and a connection test."""

from __future__ import annotations

from fastapi import APIRouter

from agenttape.api.deps import Actor, Ctx
from agenttape.application.schemas.runtime import HerdrStatusOut, HerdrTestOut, RunRuntimeView
from agenttape.application.services.runtime import RuntimeService

router = APIRouter(tags=["runtime"])


@router.get(
    "/runs/{run_id}/runtime",
    response_model=RunRuntimeView,
    summary="The run's runtime references (Herdr workspace, pane, agent) and attach command",
)
def run_runtime(run_id: str, ctx: Ctx) -> RunRuntimeView:
    return RuntimeService(ctx).run_view(run_id)


@router.get(
    "/runtime/herdr",
    response_model=HerdrStatusOut,
    summary="Herdr installed/reachable, version, protocol, capabilities, machines, integrations",
)
async def herdr_status(ctx: Ctx) -> HerdrStatusOut:
    return await RuntimeService(ctx).herdr_status()


@router.post(
    "/runtime/herdr/test",
    response_model=HerdrTestOut,
    summary="Test the Herdr connection and return actionable diagnostics",
)
async def herdr_test(ctx: Ctx, actor: Actor) -> HerdrTestOut:
    return await RuntimeService(ctx).test_connection(actor=actor)
