"""File Evidence (M2): a run's file ledger, per-file timelines, exports, and project activity."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response

from agenttape.api.deps import Actor, Ctx, Paging
from agenttape.application.schemas.common import Page
from agenttape.file_evidence.schemas import (
    FileRunHistoryItem,
    FileTimeline,
    ProjectFileActivity,
    RebuildResult,
    RunFileEvidenceSummary,
    RunFileOut,
    SortKey,
)
from agenttape.file_evidence.service import FileEvidenceService, FileFilters

router = APIRouter(tags=["file-evidence"])

ListParam = Annotated[list[str] | None, Query()]


def _filters(
    classification: ListParam = None,
    operation: ListParam = None,
    net_status: ListParam = None,
    source: ListParam = None,
    step: str | None = None,
    agent: str | None = None,
    protected: bool | None = None,
    outside: bool | None = None,
    generated: bool | None = None,
    net_unchanged: bool | None = None,
    q: Annotated[str | None, Query(max_length=500)] = None,
    sort: SortKey = "path",
    order: Literal["asc", "desc"] = "asc",
) -> FileFilters:
    """Ledger filters shared by the list and export endpoints (UI-equivalent)."""
    return FileFilters(
        classification=classification,
        operation=operation,
        net_status=net_status,
        source=source,
        step=step,
        agent=agent,
        protected=protected,
        outside=outside,
        generated=generated,
        net_unchanged=net_unchanged,
        q=q,
        sort=sort,
        order=order,
    )


Filters = Annotated[FileFilters, Depends(_filters)]


@router.get(
    "/runs/{run_id}/files",
    response_model=Page[RunFileOut],
    summary="The run's file ledger: one row per logical file, filtered, searched, sorted",
)
def list_run_files(
    run_id: str,
    ctx: Ctx,
    paging: Paging,
    filters: Filters,
) -> Page[RunFileOut]:
    return FileEvidenceService(ctx).list_files(
        run_id, filters, limit=paging.limit, offset=paging.offset
    )


@router.get(
    "/runs/{run_id}/files/summary",
    response_model=RunFileEvidenceSummary,
    summary="File evidence counts, completeness statement, collapsed noise, and findings",
)
def run_files_summary(run_id: str, ctx: Ctx) -> RunFileEvidenceSummary:
    return FileEvidenceService(ctx).summary(run_id)


@router.get(
    "/runs/{run_id}/files/timeline",
    response_model=FileTimeline,
    summary="One file's chronological evidence trail, states, final diff, and linked records",
)
def run_file_timeline(
    run_id: str, ctx: Ctx, path: Annotated[str, Query(min_length=1, max_length=4096)]
) -> FileTimeline:
    return FileEvidenceService(ctx).timeline(run_id, path)


@router.get(
    "/runs/{run_id}/files/export",
    summary="Sanitized export of the (filtered) file ledger as CSV or JSON",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}, "application/json": {}}}},
)
def export_run_files(
    run_id: str,
    ctx: Ctx,
    filters: Filters,
    format: Literal["csv", "json"] = "csv",
) -> Response:
    body, media, name = FileEvidenceService(ctx).export_ledger(run_id, filters, format)
    return Response(
        content=body,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get(
    "/runs/{run_id}/evidence/export",
    summary="Sanitized evidence bundle: completeness, Git metadata, ledger, raw evidence, "
    "collapsed noise, findings, runtime references, and context summary (JSON); or the raw "
    "evidence events (CSV)",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}, "application/json": {}}}},
)
def export_evidence_bundle(
    run_id: str, ctx: Ctx, format: Literal["csv", "json"] = "json"
) -> Response:
    body, media, name = FileEvidenceService(ctx).evidence_bundle(run_id, format)
    return Response(
        content=body,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.post(
    "/runs/{run_id}/files/rebuild",
    response_model=RebuildResult,
    summary="Rebuild the run's file summaries from stored evidence (optionally re-reconcile "
    "against the workdir's current Git state); audited",
)
def rebuild_run_files(
    run_id: str, ctx: Ctx, actor: Actor, reconcile: bool = False
) -> RebuildResult:
    return FileEvidenceService(ctx).rebuild(run_id, actor=actor, reconcile=reconcile)


@router.get(
    "/projects/{project_id}/file-activity",
    response_model=ProjectFileActivity,
    summary="Cross-run file activity: most touched, failed-run files, changed together, "
    "protected activity, recent activity (each linked to its runs)",
)
def project_file_activity(project_id: str, ctx: Ctx) -> ProjectFileActivity:
    return FileEvidenceService(ctx).project_activity(project_id)


@router.get(
    "/projects/{project_id}/file-activity/history",
    response_model=Page[FileRunHistoryItem],
    summary="One file's AgentTape run history in a project",
)
def project_file_history(
    project_id: str,
    ctx: Ctx,
    paging: Paging,
    path: Annotated[str, Query(min_length=1, max_length=4096)],
) -> Page[FileRunHistoryItem]:
    return FileEvidenceService(ctx).file_history(
        project_id, path, limit=paging.limit, offset=paging.offset
    )
