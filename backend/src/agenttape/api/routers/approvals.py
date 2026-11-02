"""Approval requests: list, get, approve, reject."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from agenttape.api.deps import Actor, Ctx, Paging
from agenttape.application.schemas.catalog import ApprovalDecision, ApprovalOut
from agenttape.application.schemas.common import Page
from agenttape.application.services.approvals import ApprovalService

router = APIRouter(tags=["approvals"])


@router.get("/approvals", response_model=Page[ApprovalOut])
def list_approvals(
    ctx: Ctx,
    paging: Paging,
    status: Annotated[list[str] | None, Query()] = None,
    run_id: str | None = None,
) -> Page[ApprovalOut]:
    return ApprovalService(ctx).list_approvals(
        status=status, run_id=run_id, limit=paging.limit, offset=paging.offset
    )


@router.get("/approvals/{approval_id}", response_model=ApprovalOut)
def get_approval(approval_id: str, ctx: Ctx) -> ApprovalOut:
    return ApprovalService(ctx).get(approval_id)


@router.post("/approvals/{approval_id}/approve", response_model=ApprovalOut)
def approve(approval_id: str, body: ApprovalDecision, ctx: Ctx, actor: Actor) -> ApprovalOut:
    reviewer = body.reviewer if body.reviewer != "local user" else actor
    return ApprovalService(ctx).approve(approval_id, reviewer=reviewer, note=body.note)


@router.post("/approvals/{approval_id}/reject", response_model=ApprovalOut)
def reject(approval_id: str, body: ApprovalDecision, ctx: Ctx, actor: Actor) -> ApprovalOut:
    reviewer = body.reviewer if body.reviewer != "local user" else actor
    return ApprovalService(ctx).reject(approval_id, reviewer=reviewer, note=body.note)
