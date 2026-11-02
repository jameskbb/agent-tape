"""Append-only audit log for material actions (approvals, cancels, config changes...)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agenttape.application.schemas.catalog import AuditEntryOut
from agenttape.application.schemas.common import Page
from agenttape.application.services.common import paginate
from agenttape.infrastructure.clock import Clock
from agenttape.infrastructure.db.models import AuditEntry
from agenttape.security.redaction import DEFAULT_REDACTOR


def record_audit(
    session: Session,
    clock: Clock,
    *,
    action: str,
    resource_type: str,
    resource_id: str | None,
    actor: str = "local user",
    details: dict[str, Any] | None = None,
    run_id: str | None = None,
    workspace_id: str | None = None,
) -> AuditEntry:
    """Append one audit entry inside the caller's transaction (never updated or deleted)."""
    entry = AuditEntry(
        workspace_id=workspace_id,
        ts=clock.now(),
        actor=actor[:200],
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        run_id=run_id,
        details=DEFAULT_REDACTOR.redact_value(details or {}),
    )
    session.add(entry)
    return entry


def list_audit(
    session: Session,
    *,
    resource_type: str | None = None,
    resource_id: str | None = None,
    run_id: str | None = None,
    action: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> Page[AuditEntryOut]:
    stmt = select(AuditEntry)
    if resource_type:
        stmt = stmt.where(AuditEntry.resource_type == resource_type)
    if resource_id:
        stmt = stmt.where(AuditEntry.resource_id == resource_id)
    if run_id:
        stmt = stmt.where(AuditEntry.run_id == run_id)
    if action:
        stmt = stmt.where(AuditEntry.action == action)
    stmt = stmt.order_by(AuditEntry.ts.desc(), AuditEntry.id.desc())
    return paginate(session, stmt, limit=limit, offset=offset, mapper=AuditEntryOut.model_validate)
