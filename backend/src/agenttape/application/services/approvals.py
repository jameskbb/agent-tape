"""Approval requests: request, approve, reject, list, with an audit entry per decision.

Resolution side effects are pluggable: each request names a ``resume_handler``. The
built-in ``run`` handler re-queues a run paused in ``waiting_for_approval`` (the adapter's
checkpoint lets it continue without redoing completed work) or fails it on rejection.
The workflow engine registers its own handler (e.g. ``workflow_node``) with
:func:`register_approval_handler`. Expiry of overdue requests is exposed through
:meth:`ApprovalService.expire_overdue` for the scheduler/worker loops to call.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from agenttape.application.context import AppContext
from agenttape.application.events import EventRecorder
from agenttape.application.schemas.catalog import ApprovalOut
from agenttape.application.schemas.common import Page
from agenttape.application.services.audit import record_audit
from agenttape.application.services.common import get_or_404, paginate
from agenttape.application.transitions import transition_attempt, transition_run
from agenttape.domain.enums import (
    ApprovalStatus,
    EventType,
    FailureCategory,
    RiskLevel,
    RunStatus,
    Severity,
)
from agenttape.domain.errors import ConflictError
from agenttape.domain.events import EventDraft
from agenttape.infrastructure.db.models import ApprovalRequest, Run, RunAttempt

ApprovalHandler = Callable[[AppContext, Session, ApprovalRequest, bool], None]
"""(ctx, session, approval, approved) -> None; runs inside the decision's transaction."""

_HANDLERS: dict[str, ApprovalHandler] = {}


def register_approval_handler(name: str, handler: ApprovalHandler) -> None:
    _HANDLERS[name] = handler


def _run_handler(
    ctx: AppContext, session: Session, approval: ApprovalRequest, approved: bool
) -> None:
    """Built-in: resume or fail a run that is ``waiting_for_approval``."""
    if approval.run_id is None:
        return
    from agenttape.application.services.runs import RunService

    run = session.get(Run, approval.run_id)
    if run is None or run.status != RunStatus.WAITING_FOR_APPROVAL.value:
        return
    recorder = EventRecorder(ctx.settings, ctx.clock)
    now = ctx.clock.now()
    if approved:
        transition_run(session, run, RunStatus.QUEUED, queued_at=now)
        recorder.record(
            session,
            run.id,
            EventType.LIFECYCLE,
            "Run queued to resume after approval",
            {"status": "queued"},
        )
        RunService(ctx).enqueue(session, run)
        return
    category = (
        FailureCategory.APPROVAL_EXPIRED
        if approval.status == ApprovalStatus.EXPIRED.value
        else FailureCategory.APPROVAL_REJECTED
    )
    message = (
        "Approval expired before a decision"
        if category == FailureCategory.APPROVAL_EXPIRED
        else f"Approval rejected by {approval.reviewer}"
        + (f": {approval.reviewer_note}" if approval.reviewer_note else "")
    )
    if approval.attempt_id:
        attempt = session.get(RunAttempt, approval.attempt_id)
        if attempt is not None and attempt.status == RunStatus.WAITING_FOR_APPROVAL.value:
            transition_attempt(
                session,
                attempt,
                RunStatus.FAILED,
                failure_category=category.value,
                failure_message=message,
                finished_at=now,
            )
    start = run.started_at or run.created_at
    transition_run(
        session,
        run,
        RunStatus.FAILED,
        failure_category=category.value,
        failure_message=message,
        finished_at=now,
        duration_ms=int((now - start).total_seconds() * 1000),
    )
    recorder.record(
        session,
        run.id,
        EventType.LIFECYCLE,
        "Run failed",
        {"status": "failed", "failure_category": category.value},
        severity=Severity.ERROR,
    )


register_approval_handler("run", _run_handler)
register_approval_handler("none", lambda *_: None)


class ApprovalService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.events = EventRecorder(ctx.settings, ctx.clock)

    def request(
        self,
        session: Session,
        *,
        workspace_id: str,
        title: str,
        rationale: str | None,
        type: str = "run_continue",
        risk_level: RiskLevel | str = RiskLevel.MEDIUM,
        requested_action: dict[str, Any] | None = None,
        run_id: str | None = None,
        attempt_id: str | None = None,
        step_run_id: str | None = None,
        node_key: str | None = None,
        resume_handler: str = "run",
        requester: str = "agenttape",
        expires_in_seconds: float | None = None,
        is_demo: bool = False,
    ) -> ApprovalRequest:
        """Create a pending approval inside the caller's transaction."""
        if resume_handler not in _HANDLERS:
            raise ConflictError(f"Unknown approval handler '{resume_handler}'")
        now = self.ctx.clock.now()
        approval = ApprovalRequest(
            workspace_id=workspace_id,
            run_id=run_id,
            attempt_id=attempt_id,
            step_run_id=step_run_id,
            node_key=node_key,
            type=type,
            title=title[:300],
            rationale=rationale,
            risk_level=RiskLevel(risk_level).value,
            requested_action=requested_action or {},
            status=ApprovalStatus.PENDING.value,
            resume_handler=resume_handler,
            requester=requester,
            expires_at=now + timedelta(seconds=expires_in_seconds) if expires_in_seconds else None,
            is_demo=is_demo,
            created_at=now,
            updated_at=now,
        )
        session.add(approval)
        session.flush()
        if run_id:
            self.events.record(
                session,
                run_id,
                EventType.APPROVAL_REQUESTED,
                f"Approval requested: {approval.title}",
                {
                    "approval_id": approval.id,
                    "type": type,
                    "risk_level": approval.risk_level,
                    "rationale": rationale,
                    "requested_action": approval.requested_action,
                    "expires_at": approval.expires_at.isoformat() if approval.expires_at else None,
                },
                attempt_id=attempt_id,
                step_run_id=step_run_id,
                node_key=node_key,
                severity=Severity.NOTICE,
            )
        record_audit(
            session,
            self.ctx.clock,
            action="approval.request",
            resource_type="approval",
            resource_id=approval.id,
            actor=requester,
            run_id=run_id,
            workspace_id=workspace_id,
            details={"type": type, "title": approval.title, "risk_level": approval.risk_level},
        )
        return approval

    def approve(self, approval_id: str, *, reviewer: str, note: str | None = None) -> ApprovalOut:
        return self._decide(approval_id, approved=True, reviewer=reviewer, note=note)

    def reject(self, approval_id: str, *, reviewer: str, note: str | None = None) -> ApprovalOut:
        return self._decide(approval_id, approved=False, reviewer=reviewer, note=note)

    def _decide(
        self, approval_id: str, *, approved: bool, reviewer: str, note: str | None
    ) -> ApprovalOut:
        with self.ctx.session() as session:
            approval = get_or_404(session, ApprovalRequest, approval_id, "approval")
            target = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
            now = self.ctx.clock.now()
            if (
                approval.expires_at is not None
                and approval.expires_at <= now
                and approval.status == ApprovalStatus.PENDING.value
            ):
                self._expire_one(session, approval)
                raise ConflictError(
                    "This approval request has expired", details={"status": "expired"}
                )
            # Conditional update: two reviewers racing cannot both decide.
            result = session.execute(
                update(ApprovalRequest)
                .where(
                    ApprovalRequest.id == approval.id,
                    ApprovalRequest.status == ApprovalStatus.PENDING.value,
                )
                .values(
                    status=target.value,
                    reviewer=reviewer,
                    reviewer_note=note,
                    resolved_at=now,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if (result.rowcount or 0) != 1:  # type: ignore[attr-defined]
                session.refresh(approval)
                raise ConflictError(
                    f"Approval is already {approval.status}", details={"status": approval.status}
                )
            session.refresh(approval)
            self._after_decision(session, approval, approved=approved, reviewer=reviewer, note=note)
            return ApprovalOut.model_validate(approval)

    def _after_decision(
        self,
        session: Session,
        approval: ApprovalRequest,
        *,
        approved: bool,
        reviewer: str,
        note: str | None,
    ) -> None:
        if approval.run_id:
            etype = (
                EventType.APPROVAL_GRANTED
                if approved
                else EventType.APPROVAL_EXPIRED
                if approval.status == ApprovalStatus.EXPIRED.value
                else EventType.APPROVAL_REJECTED
            )
            self.events.append(
                session,
                approval.run_id,
                [
                    EventDraft(
                        type=etype,
                        summary=f"Approval {approval.status}: {approval.title}",
                        payload={"approval_id": approval.id, "reviewer": reviewer, "note": note},
                        attempt_id=approval.attempt_id,
                        step_run_id=approval.step_run_id,
                        node_key=approval.node_key,
                        severity=Severity.INFO if approved else Severity.WARNING,
                    )
                ],
            )
        record_audit(
            session,
            self.ctx.clock,
            action=f"approval.{approval.status}",
            resource_type="approval",
            resource_id=approval.id,
            actor=reviewer,
            run_id=approval.run_id,
            workspace_id=approval.workspace_id,
            details={"note": note, "type": approval.type, "title": approval.title},
        )
        handler = _HANDLERS.get(approval.resume_handler)
        if handler is not None:
            handler(self.ctx, session, approval, approved)

    def _expire_one(self, session: Session, approval: ApprovalRequest) -> bool:
        now = self.ctx.clock.now()
        result = session.execute(
            update(ApprovalRequest)
            .where(
                ApprovalRequest.id == approval.id,
                ApprovalRequest.status == ApprovalStatus.PENDING.value,
            )
            .values(
                status=ApprovalStatus.EXPIRED.value,
                resolved_at=now,
                reviewer="agenttape",
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if (result.rowcount or 0) != 1:  # type: ignore[attr-defined]
            return False
        session.refresh(approval)
        self._after_decision(
            session, approval, approved=False, reviewer="agenttape", note="Expired"
        )
        return True

    def expire_overdue(self, now: datetime | None = None) -> int:
        """Expire pending approvals past ``expires_at``. Safe to call from any loop."""
        cutoff = now or self.ctx.clock.now()
        with self.ctx.session() as session:
            overdue = session.scalars(
                select(ApprovalRequest).where(
                    ApprovalRequest.status == ApprovalStatus.PENDING.value,
                    ApprovalRequest.expires_at.is_not(None),
                    ApprovalRequest.expires_at <= cutoff,
                )
            ).all()
            return sum(1 for a in overdue if self._expire_one(session, a))

    def get(self, approval_id: str) -> ApprovalOut:
        with self.ctx.session() as session:
            return ApprovalOut.model_validate(
                get_or_404(session, ApprovalRequest, approval_id, "approval")
            )

    def list_approvals(
        self,
        *,
        status: list[str] | None = None,
        run_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Page[ApprovalOut]:
        with self.ctx.session() as session:
            stmt = select(ApprovalRequest)
            if status:
                stmt = stmt.where(ApprovalRequest.status.in_(status))
            if run_id:
                stmt = stmt.where(ApprovalRequest.run_id == run_id)
            stmt = stmt.order_by(ApprovalRequest.created_at.desc(), ApprovalRequest.id.desc())
            return paginate(
                session, stmt, limit=limit, offset=offset, mapper=ApprovalOut.model_validate
            )
