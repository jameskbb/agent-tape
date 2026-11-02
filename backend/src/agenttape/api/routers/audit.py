"""Read-only audit log."""

from __future__ import annotations

from fastapi import APIRouter

from agenttape.api.deps import Ctx, Paging
from agenttape.application.schemas.catalog import AuditEntryOut
from agenttape.application.schemas.common import Page
from agenttape.application.services.audit import list_audit

router = APIRouter(tags=["audit"])


@router.get("/audit", response_model=Page[AuditEntryOut])
def get_audit(
    ctx: Ctx,
    paging: Paging,
    resource_type: str | None = None,
    resource_id: str | None = None,
    run_id: str | None = None,
    action: str | None = None,
) -> Page[AuditEntryOut]:
    with ctx.session() as session:
        return list_audit(
            session,
            resource_type=resource_type,
            resource_id=resource_id,
            run_id=run_id,
            action=action,
            limit=paging.limit,
            offset=paging.offset,
        )
