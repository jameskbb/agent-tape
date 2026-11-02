"""Persistence of Herdr runtime identity (``runtime_sessions``) and restart reattachment.

Adapters never get a database handle, so the worker hands the Herdr adapter a
:class:`RuntimeStore` for its attempt on ``ExecutionContext.runtime_store``.

:func:`requeue_for_reattach` is used by stale-work reconciliation and by the worker's
shutdown path: an attempt whose Herdr session is still recorded as active is not failed;
it is parked (attempt ``waiting_for_approval`` with a ``herdr_reattach`` checkpoint, run
``queued``) so the next worker reopens the same attempt and the adapter reattaches.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update

from agenttape.application.events import EventRecorder
from agenttape.application.services.runs import RunService
from agenttape.application.transitions import transition_attempt, transition_run
from agenttape.domain.enums import EventType, QueueItemStatus, RunStatus, Severity
from agenttape.domain.events import EventDraft
from agenttape.infrastructure.db.extra_models.runtime_sessions import RuntimeSession
from agenttape.infrastructure.db.models import Event, QueueItem, Run, RunAttempt

if TYPE_CHECKING:
    from agenttape.application.context import AppContext

#: Row statuses that mean "Herdr may still be running this attempt's agent".
LIVE_STATUSES = ("active",)
ROW_FIELDS = frozenset(c.name for c in RuntimeSession.__table__.columns) - {
    "id",
    "run_id",
    "attempt_id",
    "created_at",
    "updated_at",
}


class RuntimeStore:
    """Synchronous row access for one attempt (call through ``asyncio.to_thread``)."""

    def __init__(self, ctx: AppContext, run_id: str, attempt_id: str) -> None:
        self.ctx = ctx
        self.run_id = run_id
        self.attempt_id = attempt_id

    def load(self) -> dict[str, Any] | None:
        with self.ctx.session() as session:
            row = session.scalars(
                select(RuntimeSession).where(RuntimeSession.attempt_id == self.attempt_id)
            ).first()
            return None if row is None else _row_dict(row)

    def save(self, **fields: Any) -> None:
        unknown = set(fields) - ROW_FIELDS
        if unknown:
            raise ValueError(f"unknown runtime_sessions fields: {sorted(unknown)}")
        with self.ctx.session() as session:
            row = session.scalars(
                select(RuntimeSession).where(RuntimeSession.attempt_id == self.attempt_id)
            ).first()
            if row is None:
                row = RuntimeSession(
                    run_id=self.run_id,
                    attempt_id=self.attempt_id,
                    runtime=fields.pop("runtime", "herdr"),
                    status=fields.pop("status", "active"),
                    details={},
                )
                session.add(row)
            for key, value in fields.items():
                if key == "details":
                    value = {**(row.details or {}), **value}
                setattr(row, key, value)

    def existing_event_ids(self, ids: list[str]) -> set[str]:
        if not ids:
            return set()
        with self.ctx.session() as session:
            return set(session.scalars(select(Event.id).where(Event.id.in_(ids))))

    def now(self) -> datetime:
        return self.ctx.clock.now()


def _row_dict(row: RuntimeSession) -> dict[str, Any]:
    return {c.name: getattr(row, c.name) for c in RuntimeSession.__table__.columns}


# --------------------------------------------------------------------------- reattach


def requeue_for_reattach(ctx: AppContext, attempt_id: str, reason: str) -> bool:
    """Park a running attempt whose Herdr session is recorded live, for reattachment.

    Returns False (and changes nothing) when the attempt has no live Herdr session, the
    run is being cancelled, or the attempt is not running; the caller then applies its
    normal interrupted/cancelled handling. Herdr itself is queried by the adapter on
    reattach: a dead, replaced, or missing session becomes a structured failure there.
    """
    recorder = EventRecorder(ctx.settings, ctx.clock)
    with ctx.session() as session:
        row = session.scalars(
            select(RuntimeSession).where(RuntimeSession.attempt_id == attempt_id)
        ).first()
        if row is None or row.runtime != "herdr" or row.status not in LIVE_STATUSES:
            return False
        attempt = session.get(RunAttempt, attempt_id)
        run = session.get(Run, row.run_id)
        if (
            attempt is None
            or run is None
            or attempt.status != RunStatus.RUNNING.value
            or run.status != RunStatus.RUNNING.value
            or run.cancel_requested_at is not None
        ):
            return False
        now = ctx.clock.now()
        offline_from = row.last_sync_at or now
        checkpoint = {
            **dict(attempt.checkpoint or {}),
            "herdr_reattach": {"offline_from": offline_from.isoformat(), "reason": reason},
        }
        transition_attempt(
            session,
            attempt,
            RunStatus.WAITING_FOR_APPROVAL,
            checkpoint=checkpoint,
            lease_expires_at=None,
        )
        transition_run(session, run, RunStatus.QUEUED, queued_at=now)
        row.status = "detached"
        recorder.append(
            session,
            run.id,
            [
                EventDraft(
                    type=EventType.RUNTIME,
                    summary="AgentTape stopped observing; the Herdr session keeps running and will be reattached",
                    payload={
                        "runtime": "herdr",
                        "action": "detach",
                        "reason": reason,
                        "offline_from": offline_from.isoformat(),
                    },
                    severity=Severity.WARNING,
                    attempt_id=attempt_id,
                )
            ],
        )
        # Settle the item the crashed or stopping worker held, so the run has exactly one
        # queue item: the one enqueued for the reattach.
        session.execute(
            update(QueueItem)
            .where(QueueItem.run_id == run.id, QueueItem.status == QueueItemStatus.LEASED)
            .values(status=QueueItemStatus.DONE, completed_at=now)
        )
        RunService(ctx).enqueue(session, run)
        return True


def reattach_or_none(ctx: AppContext, attempt_id: str, reason: str) -> bool:
    """``requeue_for_reattach`` that never raises (reconciliation must keep going)."""
    with contextlib.suppress(Exception):
        return requeue_for_reattach(ctx, attempt_id, reason)
    return False
