"""Workflow maintenance sweep: re-wake workflow runs whose wake-up was lost.

Wake-ups normally arrive through the queue (child terminal hook, approval handler, cancel
hook). They can be lost when a process dies at the wrong moment - after a child finished
but before the parent was enqueued, or while a tick held the queue item. The sweep looks
at every unfinished workflow run and enqueues the ones that have work to do and no live
queue item. Enqueuing is always safe: a tick is idempotent and serialized per run.

The sweep also expires overdue approvals (``ApprovalService.expire_overdue``), so
approvals expire even when no worker is running maintenance.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select

from agenttape.application.context import AppContext
from agenttape.application.services.approvals import ApprovalService
from agenttape.application.services.runs import RunService
from agenttape.application.services.workflows import WORKFLOW_KIND
from agenttape.domain.enums import ApprovalStatus, QueueItemStatus, RunStatus
from agenttape.domain.workflows import WorkflowDefinition, plan_ready
from agenttape.infrastructure.db.models import ApprovalRequest, QueueItem, Run, StepRun

UNFINISHED = (
    RunStatus.QUEUED.value,
    RunStatus.PREPARING.value,
    RunStatus.RUNNING.value,
    RunStatus.CANCELLING.value,
    RunStatus.WAITING_FOR_APPROVAL.value,
)
#: Cancelled workflows are re-checked for a while so cancellation reaches every child.
CANCEL_PROPAGATION_WINDOW = timedelta(hours=1)
TERMINAL = {"succeeded", "failed", "timed_out", "cancelled", "skipped"}


def _needs_tick(session: Any, run: Run, now: datetime, lease_free: bool) -> str | None:
    steps = list(session.scalars(select(StepRun).where(StepRun.run_id == run.id)))
    if run.status == RunStatus.CANCELLED.value:
        return (
            "cancelled with unfinished nodes"
            if any(s.status not in TERMINAL for s in steps)
            else None
        )
    if run.status in (
        RunStatus.QUEUED.value,
        RunStatus.PREPARING.value,
        RunStatus.CANCELLING.value,
    ):
        return run.status
    if not steps:
        return "no nodes recorded yet"
    for s in steps:
        if s.status == RunStatus.WAITING_FOR_APPROVAL.value and s.approval_request_id:
            approval = session.get(ApprovalRequest, s.approval_request_id)
            if approval is not None and approval.status != ApprovalStatus.PENDING.value:
                return f"approval for '{s.node_key}' was decided"
        if s.child_run_id and s.status in ("running", "waiting_for_approval"):
            child = session.get(Run, s.child_run_id)
            if child is None or child.status in TERMINAL:
                return f"child of '{s.node_key}' finished"
            if (child.status == "waiting_for_approval") != (s.status == "waiting_for_approval"):
                return f"child of '{s.node_key}' changed state"
        if s.status == "running" and not s.child_run_id and lease_free:
            return f"'{s.node_key}' was interrupted"
    if all(s.status in TERMINAL for s in steps):
        return "all nodes settled"
    try:
        definition = WorkflowDefinition.model_validate(
            ((run.snapshot or {}).get("workflow") or {}).get("definition") or {}
        )
    except Exception:
        return "unreadable definition"
    statuses = {s.node_key: s.status for s in steps}
    outputs = {s.node_key: s.outputs for s in steps}
    try:
        ready = plan_ready(definition, statuses, outputs, {"inputs": dict(run.inputs or {})})
    except Exception:
        return "readiness could not be evaluated"
    if ready.skip:
        return "nodes to skip"
    by_key = {s.node_key: s for s in steps}
    for key in ready.run:
        retry_at = (by_key[key].state or {}).get("retry_at")
        if not retry_at or datetime.fromisoformat(str(retry_at)) <= now:
            return f"'{key}' is ready"
    return None


def sweep_workflows(ctx: AppContext) -> dict[str, Any]:
    """Enqueue stalled workflow runs and expire overdue approvals. Idempotent."""
    now = ctx.clock.now()
    report: dict[str, Any] = {"woken": [], "expired_approvals": 0}
    report["expired_approvals"] = ApprovalService(ctx).expire_overdue(now)
    with ctx.session() as session:
        runs = list(
            session.scalars(
                select(Run).where(
                    Run.kind == WORKFLOW_KIND,
                    or_(
                        Run.status.in_(UNFINISHED),
                        and_(
                            Run.status == RunStatus.CANCELLED.value,
                            Run.finished_at >= now - CANCEL_PROPAGATION_WINDOW,
                        ),
                    ),
                )
            )
        )
        for run in runs:
            items = list(
                session.scalars(
                    select(QueueItem).where(
                        QueueItem.run_id == run.id,
                        QueueItem.status.in_(
                            [QueueItemStatus.QUEUED.value, QueueItemStatus.LEASED.value]
                        ),
                    )
                )
            )
            live = False
            for item in items:
                if item.status == QueueItemStatus.QUEUED.value or (
                    item.lease_expires_at is not None and item.lease_expires_at >= now
                ):
                    live = True
                else:
                    # A worker died holding this item; nobody will complete it.
                    item.status = QueueItemStatus.DONE.value
                    item.completed_at = now
            if live:
                continue
            lease_free = ctx.leases.holder(f"wf:{run.id}") is None
            if not lease_free:
                continue
            reason = _needs_tick(session, run, now, lease_free)
            if reason is None:
                continue
            RunService(ctx).enqueue(session, run)
            report["woken"].append({"run_id": run.id, "reason": reason})
    return report
