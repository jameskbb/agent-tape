"""Context evidence: run context summary, provenance explorer, sources, findings,
comparison, project Context Activity, and the reviewed learning loop (M2)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from agenttape.api.deps import Actor, Ctx
from agenttape.context_evidence import service

router = APIRouter(tags=["context"])

Opt = Annotated[str | None, Query(max_length=300)]


class FeedbackCreate(BaseModel):
    kind: str = "correction"
    text: str = Field(min_length=1, max_length=4000)
    source_key: str | None = Field(default=None, max_length=300)


class ClassifyBody(BaseModel):
    classification: str


class ProposeBody(BaseModel):
    destination: str
    lesson_text: str | None = Field(default=None, max_length=4000)


class ReviewBody(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


@router.get("/runs/{run_id}/context", summary="Context summary for a run")
def run_context(run_id: str, ctx: Ctx) -> dict[str, Any]:
    with ctx.session() as session:
        return service.run_summary(session, run_id)


@router.get("/runs/{run_id}/context/packs", summary="The run's immutable context packs")
def run_packs(run_id: str, ctx: Ctx) -> list[dict[str, Any]]:
    with ctx.session() as session:
        return service.list_packs(session, run_id)


@router.get(
    "/runs/{run_id}/context/evidence",
    summary="Provenance explorer: filtered, searchable, chronological context evidence",
)
def run_evidence(
    run_id: str,
    ctx: Ctx,
    stage: Opt = None,
    source_type: Opt = None,
    provider: Opt = None,
    node_key: Opt = None,
    authority: Opt = None,
    freshness: Opt = None,
    quality: Opt = None,
    source_id: Opt = None,
    q: Opt = None,
    limit: Annotated[int, Query(ge=1, le=service.MAX_PAGE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    with ctx.session() as session:
        return service.list_evidence(
            session,
            run_id,
            stage=stage,
            source_type=source_type,
            provider=provider,
            node_key=node_key,
            authority=authority,
            freshness=freshness,
            quality=quality,
            source_id=source_id,
            q=q,
            limit=limit,
            offset=offset,
        )


@router.get(
    "/runs/{run_id}/context/sources/{source_id}",
    summary="Source detail: safe locator, freshness, access history, linked findings",
)
def run_source(run_id: str, source_id: str, ctx: Ctx) -> dict[str, Any]:
    with ctx.session() as session:
        return service.source_detail(session, run_id, source_id)


@router.get(
    "/runs/{run_id}/context/findings",
    summary="Findings with their supporting, conflicting, and missing sources",
)
def run_findings(run_id: str, ctx: Ctx) -> list[dict[str, Any]]:
    with ctx.session() as session:
        return service.findings(session, run_id)


@router.get(
    "/runs/{run_id}/context/compare/{other_id}",
    summary="How two runs' supplied and retrieved context differed",
)
def run_context_compare(run_id: str, other_id: str, ctx: Ctx) -> dict[str, Any]:
    with ctx.session() as session:
        return service.compare(session, run_id, other_id)


@router.get(
    "/projects/{project_id}/context/activity",
    summary="Project Context Activity: retrieval, citations, gaps, and approved lessons",
)
def project_activity(project_id: str, ctx: Ctx) -> dict[str, Any]:
    with ctx.session() as session:
        return service.project_activity(session, project_id)


@router.get("/context/feedback", summary="Context feedback (gaps, corrections, lessons)")
def feedback_list(
    ctx: Ctx, run_id: Opt = None, project_id: Opt = None, status: Opt = None
) -> list[dict[str, Any]]:
    with ctx.session() as session:
        return service.list_feedback(session, run_id=run_id, project_id=project_id, status=status)


@router.post(
    "/runs/{run_id}/context/feedback",
    status_code=201,
    summary="Record a correction or lesson candidate on a run (pending review)",
)
def feedback_create(run_id: str, body: FeedbackCreate, ctx: Ctx, actor: Actor) -> dict[str, Any]:
    with ctx.session() as session:
        return service.create_feedback(
            session,
            ctx.clock,
            run_id,
            kind=body.kind,
            text=body.text,
            source_key=body.source_key,
            actor=actor,
        )


@router.post("/context/feedback/{feedback_id}/classify", summary="Classify feedback")
def feedback_classify(
    feedback_id: str, body: ClassifyBody, ctx: Ctx, actor: Actor
) -> dict[str, Any]:
    with ctx.session() as session:
        return service.classify(session, ctx.clock, feedback_id, body.classification, actor=actor)


@router.post(
    "/context/feedback/{feedback_id}/propose",
    summary="Change the proposed destination or lesson text before approval",
)
def feedback_propose(feedback_id: str, body: ProposeBody, ctx: Ctx, actor: Actor) -> dict[str, Any]:
    with ctx.session() as session:
        return service.propose(
            session, ctx.clock, feedback_id, body.destination, body.lesson_text, actor=actor
        )


@router.post(
    "/context/feedback/{feedback_id}/approve",
    summary="Approve the proposal (a human decision; nothing is promoted automatically)",
)
def feedback_approve(
    feedback_id: str, ctx: Ctx, actor: Actor, body: ReviewBody | None = None
) -> dict[str, Any]:
    with ctx.session() as session:
        return service.approve(
            session, ctx.clock, feedback_id, actor=actor, note=body.note if body else None
        )


@router.post("/context/feedback/{feedback_id}/reject", summary="Reject the feedback")
def feedback_reject(
    feedback_id: str, ctx: Ctx, actor: Actor, body: ReviewBody | None = None
) -> dict[str, Any]:
    with ctx.session() as session:
        return service.reject(
            session, ctx.clock, feedback_id, actor=actor, note=body.note if body else None
        )
