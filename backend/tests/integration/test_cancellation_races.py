"""Cancellation races and recovery.

The core already implements cancellation, timeouts, retry with backoff, and stale-run
recovery (see test_worker.py); these tests cover the races between them.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time

from sqlalchemy import select

from agenttape.application.context import AppContext
from agenttape.application.services.runs import RunService
from agenttape.domain.enums import TERMINAL_STATUSES, QueueItemStatus, RunStatus
from agenttape.domain.errors import ConflictError
from agenttape.infrastructure.db.models import AuditEntry, QueueItem, RunAttempt
from agenttape.worker.reconcile import reconcile_stale
from agenttape.worker.registry import WorkerRuntime
from agenttape.worker.worker import Worker
from tests.conftest import demo_profile, drive, start_run


def attempts(ctx: AppContext, run_id: str) -> list[RunAttempt]:
    with ctx.session() as session:
        return list(session.scalars(select(RunAttempt).where(RunAttempt.run_id == run_id)))


def queue_items(ctx: AppContext, run_id: str) -> list[QueueItem]:
    with ctx.session() as session:
        return list(session.scalars(select(QueueItem).where(QueueItem.run_id == run_id)))


def test_cancel_between_claim_and_prepare(ctx: AppContext) -> None:
    run_id = start_run(ctx, demo_profile(ctx))
    worker = Worker(ctx, worker_id="w")
    claim = ctx.queue.claim("w", 30)
    assert claim is not None and claim.run_id == run_id
    RunService(ctx).cancel_run(run_id, reason="changed my mind")
    asyncio.run(worker.executors["agent"].execute(WorkerRuntime(ctx=ctx, worker_id="w"), claim))
    run = RunService(ctx).get_run(run_id)
    assert run.status == RunStatus.CANCELLED
    assert attempts(ctx, run_id) == []
    assert {i.status for i in queue_items(ctx, run_id)} == {QueueItemStatus.DONE.value}


def test_cancel_queued_run_is_never_claimed(ctx: AppContext) -> None:
    run_id = start_run(ctx, demo_profile(ctx))
    RunService(ctx).cancel_run(run_id)
    assert ctx.queue.claim("w", 30) is None
    assert {i.status for i in queue_items(ctx, run_id)} == {QueueItemStatus.CANCELLED.value}


def test_concurrent_cancels_settle_once(ctx: AppContext) -> None:
    for _ in range(5):
        run_id = start_run(ctx, demo_profile(ctx, name=f"c{time.monotonic_ns()}"))
        outcomes: list[BaseException | None] = []
        barrier = threading.Barrier(3)

        def cancel(rid: str, gate: threading.Barrier, out: list[BaseException | None]) -> None:
            try:
                gate.wait()
                RunService(ctx).cancel_run(rid)
                out.append(None)
            except BaseException as exc:  # every outcome is recorded; nothing is dropped
                out.append(exc)

        threads = [
            threading.Thread(target=cancel, args=(run_id, barrier, outcomes)) for _ in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(outcomes) == 3
        # The only acceptable failure is a clean conflict for a loser of the race.
        unexpected = [o for o in outcomes if o is not None and not isinstance(o, ConflictError)]
        assert unexpected == [], unexpected
        assert sum(o is None for o in outcomes) >= 1
        assert RunService(ctx).get_run(run_id).status == RunStatus.CANCELLED
        cancellations = RunService(ctx).list_events(run_id, types=["cancellation"]).items
        assert len(cancellations) == 1
        with ctx.session() as session:
            audits = session.scalars(
                select(AuditEntry).where(
                    AuditEntry.run_id == run_id, AuditEntry.action == "run.cancel"
                )
            ).all()
        assert len(audits) == 1


def test_cancel_racing_completion_always_settles(ctx: AppContext) -> None:
    """Cancel at varying moments while the Demo Agent runs: the run always ends in one
    consistent terminal state, its attempt is closed, and its queue item is done."""
    profile = demo_profile(ctx)
    for delay in (0.0, 0.02, 0.05, 0.1, 0.2):
        run_id = start_run(ctx, profile)
        stopper = threading.Timer(delay, lambda rid=run_id: _try_cancel(ctx, rid))
        stopper.start()
        detail = drive(ctx, run_id, timeout=60)
        stopper.join()
        assert RunStatus(detail.run.status) in TERMINAL_STATUSES
        assert detail.run.status in ("cancelled", "succeeded")
        for a in detail.attempts:
            assert RunStatus(a.status) in TERMINAL_STATUSES
        assert all(i.status != QueueItemStatus.LEASED.value for i in queue_items(ctx, run_id))
        if detail.run.status == "cancelled":
            assert detail.run.failure_category == "cancelled"


def _try_cancel(ctx: AppContext, run_id: str) -> None:
    with contextlib.suppress(ConflictError):  # already finished: nothing to cancel
        RunService(ctx).cancel_run(run_id, reason="race")


def test_retry_after_cancel_starts_a_new_attempt(ctx: AppContext) -> None:
    run_id = start_run(ctx, demo_profile(ctx))
    RunService(ctx).cancel_run(run_id)
    RunService(ctx).retry_run(run_id)
    detail = drive(ctx, run_id)
    assert detail.run.status == "succeeded"
    assert len(detail.attempts) == 1


def test_worker_crash_mid_attempt_is_recovered_and_retried(ctx: AppContext) -> None:
    """A worker that dies mid-attempt leaves a leased item and a running attempt; after the
    lease expires reconciliation interrupts the attempt and the retry policy re-queues it."""
    run_id = start_run(ctx, demo_profile(ctx, max_attempts=2))
    claim = ctx.queue.claim("dead-worker", 0.05)
    assert claim is not None
    runtime = WorkerRuntime(ctx=ctx, worker_id="dead-worker")
    executor = Worker(ctx, worker_id="x").executors["agent"]
    prepared = executor._prepare(runtime, claim)  # type: ignore[attr-defined]
    assert prepared is not None
    with ctx.session() as session:
        attempt = session.get(RunAttempt, prepared.attempt_id)
        assert attempt is not None
        attempt.lease_expires_at = ctx.clock.now()
    time.sleep(0.1)
    report = reconcile_stale(ctx)
    assert run_id in report["requeued"]
    detail = drive(ctx, run_id)
    assert detail.run.status == "succeeded"
    assert [a.status for a in detail.attempts] == ["failed", "succeeded"]
    assert detail.attempts[0].failure_category == "interrupted"
