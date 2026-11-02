"""Stale-work reconciliation: recovering attempts whose worker went away.

An attempt in ``preparing``/``running``/``cancelling`` whose lease expired has lost its
owner (the worker crashed, was killed, or the machine slept). We never assume a stored
PID still belongs to that attempt; ownership is proven only by a live lease. Stale
attempts become ``failed`` with category ``interrupted`` (or ``cancelled`` when a cancel was
requested), and the run is re-queued if its retry policy allows, otherwise failed. The
exception is an attempt whose agent lives in Herdr with a session recorded as active: it
is re-queued for reattachment to that session (``herdr/sessions.py``).

Leased queue items whose lease expired are settled too: re-opened if their run is still
``queued`` (the worker died between claim and start), otherwise marked done.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select

from agenttape.application.context import AppContext
from agenttape.application.events import EventRecorder
from agenttape.application.services.runs import RunService
from agenttape.application.transitions import (
    ConcurrentUpdateError,
    transition_attempt,
    transition_run,
)
from agenttape.domain.enums import (
    EventType,
    FailureCategory,
    QueueItemStatus,
    RunStatus,
    Severity,
)
from agenttape.domain.events import EventDraft
from agenttape.domain.retry import RetryPolicy
from agenttape.herdr.sessions import reattach_or_none
from agenttape.infrastructure.db.models import Run, RunAttempt

STALE_ATTEMPT_STATUSES = (
    RunStatus.PREPARING.value,
    RunStatus.RUNNING.value,
    RunStatus.CANCELLING.value,
)


def reconcile_stale(ctx: AppContext) -> dict[str, Any]:
    """Settle attempts and queue items whose leases expired. Idempotent; safe to run often."""
    now = ctx.clock.now()
    report: dict[str, Any] = {
        "interrupted": [],
        "requeued": [],
        "cancelled": [],
        "queue_reopened": 0,
        "reattach": [],
    }
    with ctx.session() as session:
        stale_ids = list(
            session.scalars(
                select(RunAttempt.id).where(
                    RunAttempt.status.in_(STALE_ATTEMPT_STATUSES),
                    RunAttempt.lease_expires_at.is_not(None),
                    RunAttempt.lease_expires_at < now,
                )
            )
        )
    for attempt_id in stale_ids:
        # A Herdr-hosted agent outlives its worker: park it for reattachment instead.
        if reattach_or_none(ctx, attempt_id, "worker_lease_expired"):
            report["reattach"].append(attempt_id)
            continue
        try:
            outcome = _reconcile_attempt(ctx, attempt_id)
        except ConcurrentUpdateError:
            continue  # its owner finished it concurrently; nothing to do
        if outcome is not None:
            report[outcome[0]].append(outcome[1])
    report["queue_reopened"] = _reconcile_queue(ctx)
    return report


def _reconcile_attempt(ctx: AppContext, attempt_id: str) -> tuple[str, str] | None:
    recorder = EventRecorder(ctx.settings, ctx.clock)
    with ctx.session() as session:
        attempt = session.get(RunAttempt, attempt_id)
        if attempt is None or attempt.status not in STALE_ATTEMPT_STATUSES:
            return None
        now = ctx.clock.now()
        if attempt.lease_expires_at is None or attempt.lease_expires_at >= now:
            return None
        run = session.get(Run, attempt.run_id)
        assert run is not None
        cancel_requested = run.cancel_requested_at is not None
        message = (
            f"Attempt {attempt.attempt_number} was interrupted: its worker "
            f"({attempt.worker_id or 'unknown'}) stopped heartbeating"
        )
        transition_attempt(
            session,
            attempt,
            RunStatus.CANCELLED if cancel_requested else RunStatus.FAILED,
            failure_category=(
                FailureCategory.CANCELLED if cancel_requested else FailureCategory.INTERRUPTED
            ).value,
            failure_message=message,
            finished_at=now,
            lease_expires_at=None,
        )
        drafts = [
            EventDraft(
                type=EventType.ERROR,
                summary=message,
                payload={
                    "attempt": attempt.attempt_number,
                    "worker_id": attempt.worker_id,
                    "reconciled_at": now.isoformat(),
                },
                severity=Severity.ERROR,
                attempt_id=attempt.id,
            )
        ]
        if run.status not in STALE_ATTEMPT_STATUSES:
            recorder.append(session, run.id, drafts)
            return ("interrupted", run.id)
        start = run.started_at or run.created_at
        duration = int((now - start).total_seconds() * 1000)
        if cancel_requested:
            transition_run(
                session,
                run,
                RunStatus.CANCELLED,
                finished_at=now,
                duration_ms=duration,
                failure_category=FailureCategory.CANCELLED.value,
                failure_message=run.cancel_reason,
            )
            drafts.append(
                EventDraft(
                    type=EventType.LIFECYCLE,
                    summary="Run cancelled",
                    payload={"status": "cancelled"},
                )
            )
            recorder.append(session, run.id, drafts)
            return ("cancelled", run.id)
        policy = RetryPolicy.from_config(
            dict(run.options or {}).get("retry"),
            default_max_attempts=run.max_attempts,
            default_base=ctx.settings.retry_backoff_base_seconds,
            default_max=ctx.settings.retry_backoff_max_seconds,
        )
        policy = RetryPolicy(
            max_attempts=run.max_attempts,
            backoff_base_seconds=policy.backoff_base_seconds,
            backoff_max_seconds=policy.backoff_max_seconds,
            retry_on=policy.retry_on,
        )
        if policy.should_retry(FailureCategory.INTERRUPTED, attempt.attempt_number):
            delay = policy.backoff_seconds(attempt.attempt_number)
            transition_run(session, run, RunStatus.QUEUED, queued_at=now)
            drafts.append(
                EventDraft(
                    type=EventType.RETRY,
                    summary=f"Re-queued after interruption (attempt {attempt.attempt_number + 1} of {run.max_attempts})",
                    payload={"failure_category": "interrupted", "delay_seconds": delay},
                    severity=Severity.WARNING,
                )
            )
            recorder.append(session, run.id, drafts)
            RunService(ctx).enqueue(session, run, available_at=now + timedelta(seconds=delay))
            return ("requeued", run.id)
        transition_run(
            session,
            run,
            RunStatus.FAILED,
            finished_at=now,
            duration_ms=duration,
            failure_category=FailureCategory.INTERRUPTED.value,
            failure_message=message,
        )
        drafts.append(
            EventDraft(
                type=EventType.LIFECYCLE,
                summary="Run failed (interrupted)",
                payload={"status": "failed", "failure_category": "interrupted"},
                severity=Severity.ERROR,
            )
        )
        recorder.append(session, run.id, drafts)
        return ("interrupted", run.id)


def _reconcile_queue(ctx: AppContext) -> int:
    reopened = 0
    with ctx.session() as session:
        for item in ctx.queue.expired_leases(session):
            run = session.get(Run, item.run_id) if item.run_id else None
            if run is not None and run.status == RunStatus.QUEUED.value:
                # Worker died after claiming but before starting: make it claimable again.
                item.status = QueueItemStatus.QUEUED.value
                item.lease_owner = None
                item.lease_expires_at = None
                reopened += 1
            elif run is None or run.status not in STALE_ATTEMPT_STATUSES:
                item.status = QueueItemStatus.DONE.value
                item.completed_at = ctx.clock.now()
    return reopened
