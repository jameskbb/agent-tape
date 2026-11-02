"""The scheduler: a lease-owned loop that turns due schedule occurrences into runs.

One tick:

1. Acquire/renew the ``scheduler`` lease. Without it the instance is a standby and does
   nothing else (so two scheduler processes never both evaluate schedules).
2. Recover ``pending`` occurrences left by a crash between inserting the occurrence and
   creating its run (``RunService.create_run`` is idempotent on the occurrence's key).
3. For every enabled schedule with ``next_run_at <= now``: collect the due times, advance
   ``next_run_at`` with a compare-and-set, apply the misfire policy, insert occurrence rows
   (``UNIQUE (schedule_id, occurrence_at)`` backs up the lease), then apply the concurrency
   policy and create runs.
4. Start queued occurrences whose schedule no longer has an active run.

Nothing lives only in memory: stopping the process at any point and starting it again
(or starting a second instance) resumes from the database.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from agenttape import __version__
from agenttape.application.context import AppContext
from agenttape.application.services.audit import record_audit
from agenttape.application.services.schedules import (
    OPEN_RUN_STATUSES,
    SCHEDULER_LEASE,
    ScheduleService,
)
from agenttape.domain.errors import AgentTapeError
from agenttape.infrastructure.db.models import Run, Schedule, ScheduleOccurrence, WorkerHeartbeat
from agenttape.scheduler.calc import build_calculator
from agenttape.scheduler.policies import (
    DEFAULT_MISFIRE_GRACE_SECONDS,
    OccurrenceStatus,
    plan_due,
)

log = logging.getLogger("agenttape.scheduler")

#: Most due times enumerated per schedule per tick before fast-forwarding.
MAX_DUE_SCAN = 1000


def make_scheduler_id() -> str:
    return f"scheduler:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


class SchedulerEngine:
    def __init__(
        self,
        ctx: AppContext,
        *,
        owner_id: str | None = None,
        lease_seconds: float = 30.0,
        misfire_grace_seconds: float = DEFAULT_MISFIRE_GRACE_SECONDS,
    ) -> None:
        self.ctx = ctx
        self.owner_id = owner_id or make_scheduler_id()
        self.lease_seconds = lease_seconds
        self.grace = misfire_grace_seconds
        self.service = ScheduleService(ctx)
        self.is_leader = False
        self.started_at = ctx.clock.now()

    # ------------------------------------------------------------------ public

    def tick(self) -> dict[str, Any]:
        """One scheduling pass. Returns counters (for logs and tests)."""
        leader = self.ctx.leases.acquire(SCHEDULER_LEASE, self.owner_id, self.lease_seconds)
        if leader != self.is_leader:
            log.info("scheduler %s is now %s", self.owner_id, "leader" if leader else "standby")
        self.is_leader = leader
        self._heartbeat("running", role="leader" if leader else "standby")
        if not leader:
            return {"leader": False}
        report = self.leader_pass()
        report["leader"] = True
        return report

    def leader_pass(self, now: datetime | None = None) -> dict[str, Any]:
        """The work a lease holder does. Safe to call concurrently (tests do)."""
        report: dict[str, Any] = {"recovered": 0, "fired": 0, "missed": 0, "dispatched": 0}
        report["recovered"] = self._recover_pending()
        now = now or self.ctx.clock.now()
        with self.ctx.session() as session:
            due_ids = list(
                session.scalars(
                    select(Schedule.id)
                    .where(Schedule.enabled.is_(True), Schedule.next_run_at <= now)
                    .order_by(Schedule.next_run_at)
                )
            )
        for schedule_id in due_ids:
            try:
                fired, missed = self._process_schedule(schedule_id, now)
            except OperationalError:
                log.warning("schedule %s was busy; retrying next tick", schedule_id)
                continue
            except AgentTapeError as exc:
                log.warning("schedule %s could not be evaluated: %s", schedule_id, exc.message)
                continue
            report["fired"] += fired
            report["missed"] += missed
        report["dispatched"] = self._dispatch_queued()
        return report

    def stop(self) -> None:
        with contextlib.suppress(Exception):
            if self.is_leader:
                self.ctx.leases.release(SCHEDULER_LEASE, self.owner_id)
        with contextlib.suppress(Exception):
            self._heartbeat("stopped", role="stopped")
        self.is_leader = False

    # ------------------------------------------------------------------ internals

    def _recover_pending(self) -> int:
        with self.ctx.session() as session:
            pending = list(
                session.scalars(
                    select(ScheduleOccurrence.id)
                    .where(ScheduleOccurrence.status == OccurrenceStatus.PENDING.value)
                    .order_by(ScheduleOccurrence.occurrence_at)
                )
            )
        for occ_id in pending:
            try:
                self.service.start_or_hold(occ_id)
            except (AgentTapeError, OperationalError) as exc:
                log.warning("could not recover occurrence %s: %s", occ_id, exc)
        return len(pending)

    def _process_schedule(self, schedule_id: str, now: datetime) -> tuple[int, int]:
        fire_ids: list[str] = []
        missed_count = 0
        with self.ctx.session() as session:
            s = session.get(Schedule, schedule_id)
            if s is None or not s.enabled or s.next_run_at is None or s.next_run_at > now:
                return 0, 0
            expected_next = s.next_run_at
            calc = build_calculator(
                kind=s.kind,
                timezone=s.timezone,
                cron_expression=s.cron_expression,
                interval_seconds=s.interval_seconds,
                run_at=s.run_at,
                start_at=s.start_at,
                end_at=s.end_at,
                anchor=s.created_at,
            )
            due: list[datetime] = []
            cursor: datetime | None = expected_next
            truncated = False
            while cursor is not None and cursor <= now:
                due.append(cursor)
                if len(due) >= MAX_DUE_SCAN:
                    truncated = True
                    # Fast-forward: only the last day of due times is kept for the plan.
                    cursor = calc.next_after(now - timedelta(days=1))
                    due = []
                    while cursor is not None and cursor <= now and len(due) < MAX_DUE_SCAN:
                        due.append(cursor)
                        cursor = calc.next_after(cursor)
                    break
                cursor = calc.next_after(cursor)
            new_next = cursor if cursor is None or cursor > now else calc.next_after(now)
            # Compare-and-set: if another scheduler already advanced this schedule, stop.
            advanced = session.execute(
                update(Schedule)
                .where(Schedule.id == s.id, Schedule.next_run_at == expected_next)
                .values(next_run_at=new_next, updated_at=now)
                .execution_options(synchronize_session=False)
            )
            if (advanced.rowcount or 0) != 1:  # type: ignore[attr-defined]
                return 0, 0
            plan = plan_due(due, now, s.misfire_policy, self.grace)
            for when in plan.missed:
                self.service.insert_occurrence(
                    session,
                    s.id,
                    when,
                    OccurrenceStatus.MISSED,
                    reason=(
                        "Missed while no scheduler was running (misfire: skip)"
                        if s.misfire_policy == "skip"
                        else "Missed while no scheduler was running; coalesced into one run (misfire: run once)"
                    ),
                )
            missed_count = plan.missed_total
            if missed_count or truncated:
                record_audit(
                    session,
                    self.ctx.clock,
                    action="schedule.misfire",
                    resource_type="schedule",
                    resource_id=s.id,
                    actor="scheduler",
                    workspace_id=s.workspace_id,
                    details={
                        "policy": s.misfire_policy,
                        "missed": missed_count,
                        "more_than_scanned": truncated,
                        "fired": [d.isoformat() for d in plan.fire],
                    },
                )
            for when in plan.fire:
                occ = self.service.insert_occurrence(
                    session,
                    s.id,
                    when,
                    OccurrenceStatus.PENDING,
                    reason=(
                        "Late: coalesced missed occurrences (misfire: run once)"
                        if now - when > timedelta(seconds=self.grace)
                        else None
                    ),
                )
                if occ is not None:
                    fire_ids.append(occ.id)
        for occ_id in fire_ids:
            self.service.start_or_hold(occ_id)
        return len(fire_ids), missed_count

    def _dispatch_queued(self) -> int:
        started = 0
        with self.ctx.session() as session:
            schedule_ids = list(
                session.scalars(
                    select(ScheduleOccurrence.schedule_id)
                    .where(ScheduleOccurrence.status == OccurrenceStatus.QUEUED.value)
                    .distinct()
                )
            )
        for schedule_id in schedule_ids:
            with self.ctx.session() as session:
                open_runs = session.scalar(
                    select(Run.id)
                    .where(Run.schedule_id == schedule_id, Run.status.in_(OPEN_RUN_STATUSES))
                    .limit(1)
                )
                if open_runs is not None:
                    continue
                s = session.get(Schedule, schedule_id)
                if s is None or not s.enabled:
                    continue
                oldest = session.scalars(
                    select(ScheduleOccurrence.id)
                    .where(
                        ScheduleOccurrence.schedule_id == schedule_id,
                        ScheduleOccurrence.status == OccurrenceStatus.QUEUED.value,
                    )
                    .order_by(ScheduleOccurrence.occurrence_at)
                    .limit(1)
                ).first()
            if oldest is not None:
                try:
                    if self.service.create_run_for(oldest):
                        started += 1
                except (AgentTapeError, OperationalError) as exc:
                    log.warning("could not start queued occurrence %s: %s", oldest, exc)
        return started

    def _heartbeat(self, status: str, *, role: str) -> None:
        now = self.ctx.clock.now()
        with self.ctx.session() as session:
            hb = session.get(WorkerHeartbeat, self.owner_id)
            if hb is None:
                hb = WorkerHeartbeat(
                    worker_id=self.owner_id,
                    hostname=socket.gethostname(),
                    pid=os.getpid(),
                    kind="scheduler",
                    version=__version__,
                    status=status,
                    capacity=0,
                    active_count=0,
                    started_at=self.started_at,
                    last_heartbeat_at=now,
                    info={"role": role},
                )
                session.add(hb)
            hb.status = status
            hb.last_heartbeat_at = now
            hb.info = {"role": role}
            if status == "stopped":
                hb.stopped_at = now


class SchedulerLoop:
    """Background service: ticks the engine until ``stop`` is set."""

    name = "scheduler"

    def __init__(self, ctx: AppContext, *, interval: float = 2.0, lease_seconds: float = 30.0):
        self.ctx = ctx
        self.interval = interval
        self.engine = SchedulerEngine(ctx, lease_seconds=lease_seconds)

    async def run(self, stop: asyncio.Event) -> None:
        log.info("scheduler %s starting", self.engine.owner_id)
        try:
            while not stop.is_set():
                try:
                    await asyncio.to_thread(self.engine.tick)
                except Exception:
                    log.exception("scheduler tick failed")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self.interval)
        finally:
            await asyncio.to_thread(self.engine.stop)
            log.info("scheduler %s stopped", self.engine.owner_id)
