"""Replay, fork, lineage, side-by-side comparison, and the zip export bundle."""

from __future__ import annotations

from fastapi import APIRouter, Response

from agenttape.api.deps import Actor, Ctx
from agenttape.application.schemas.git import (
    Comparison,
    ForkRequest,
    LineageCreateResult,
    LineageOut,
    ReplayRequest,
)
from agenttape.application.services.comparison import ComparisonService
from agenttape.application.services.exports import ExportService
from agenttape.application.services.lineage import LineageService

router = APIRouter(tags=["lineage"])


@router.get(
    "/runs/{run_id}/lineage",
    response_model=LineageOut,
    summary="Parent and child runs, environment drift, and fork options",
)
def lineage(run_id: str, ctx: Ctx) -> LineageOut:
    return LineageService(ctx).lineage(run_id)


@router.post(
    "/runs/{run_id}/replay",
    response_model=LineageCreateResult,
    status_code=201,
    summary="Replay a run with its exact versions and inputs",
)
def replay(
    run_id: str, ctx: Ctx, actor: Actor, body: ReplayRequest | None = None
) -> LineageCreateResult:
    return LineageService(ctx).replay(run_id, body, actor=actor)


@router.post(
    "/runs/{run_id}/fork",
    response_model=LineageCreateResult,
    status_code=201,
    summary="Fork a run with a changed prompt version, agent profile, or variables",
)
def fork(run_id: str, body: ForkRequest, ctx: Ctx, actor: Actor) -> LineageCreateResult:
    return LineageService(ctx).fork(run_id, body, actor=actor)


@router.get(
    "/runs/{run_id}/compare/{other_id}",
    response_model=Comparison,
    summary="Compare two runs side by side",
)
def compare(run_id: str, other_id: str, ctx: Ctx) -> Comparison:
    return ComparisonService(ctx).compare(run_id, other_id)


@router.get(
    "/runs/{run_id}/export/archive",
    summary="Sanitized zip bundle: bundle.json plus the run's text artifacts",
    response_class=Response,
)
def export_archive(run_id: str, ctx: Ctx) -> Response:
    data = ExportService(ctx).run_bundle_archive(run_id)
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="agenttape-run-{run_id}.zip"'},
    )
