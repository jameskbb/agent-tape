"""The worker loop: claim queued runs, execute them concurrently, heartbeat, reconcile.

Runs in-process under ``agenttape serve`` or standalone via ``agenttape worker``. Several
workers (processes or hosts sharing a PostgreSQL database) may run at once; the queue's
leases guarantee each run has one active executor.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import uuid
from datetime import UTC, datetime

from agenttape import __version__
from agenttape.application.context import AppContext
from agenttape.application.ports import ClaimedItem
from agenttape.application.services.approvals import ApprovalService
from agenttape.infrastructure.db.models import WorkerHeartbeat
from agenttape.worker.reconcile import reconcile_stale
from agenttape.worker.registry import RunExecutor, WorkerRuntime, load_executors

log = logging.getLogger("agenttape.worker")


def make_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


class Worker:
    name = "worker"

    def __init__(
        self,
        ctx: AppContext,
        *,
        worker_id: str | None = None,
        concurrency: int | None = None,
        poll_interval: float | None = None,
        kind: str = "worker",
    ) -> None:
        self.ctx = ctx
        self.worker_id = worker_id or make_worker_id()
        self.concurrency = max(1, concurrency or ctx.settings.worker_concurrency)
        self.poll_interval = poll_interval or ctx.settings.worker_poll_interval_seconds
        self.kind = kind
        self.executors: dict[str, RunExecutor] = load_executors()
        self.runtime = WorkerRuntime(ctx=ctx, worker_id=self.worker_id)
        self._active: set[asyncio.Task[None]] = set()
        self._wake = asyncio.Event()
        self.started_at = datetime.now(UTC)

    # ------------------------------------------------------------------ heartbeat

    def _heartbeat(self, status: str = "running") -> None:
        now = self.ctx.clock.now()
        with self.ctx.session() as session:
            hb = session.get(WorkerHeartbeat, self.worker_id)
            if hb is None:
                hb = WorkerHeartbeat(
                    worker_id=self.worker_id,
                    hostname=socket.gethostname(),
                    pid=os.getpid(),
                    kind=self.kind,
                    version=__version__,
                    status=status,
                    capacity=self.concurrency,
                    active_count=0,
                    started_at=now,
                    last_heartbeat_at=now,
                    info={"executors": sorted(self.executors)},
                )
                session.add(hb)
            hb.status = status
            hb.active_count = len(self._active)
            hb.last_heartbeat_at = now
            if status == "stopped":
                hb.stopped_at = now

    def _maintenance(self) -> None:
        self._heartbeat()
        report = reconcile_stale(self.ctx)
        if report["interrupted"] or report["requeued"] or report["cancelled"]:
            log.warning("reconciled stale work: %s", report)
        ApprovalService(self.ctx).expire_overdue()

    def _backfill_findings(self) -> None:
        """Once per worker start: findings recorded before the findings table existed get
        their rows (``backfill_findings``, idempotent), so an upgraded database still
        shows them. A failure is logged and never stops the worker."""
        from agenttape.context_evidence.hooks import backfill_findings

        try:
            backfill_findings(self.ctx)
        except Exception:
            log.exception("findings back-fill failed; it is retried on the next worker start")

    # ------------------------------------------------------------------ execution

    async def _execute(self, claim: ClaimedItem) -> None:
        kind = str(claim.payload.get("run_kind") or "")
        executor = self.executors.get(kind) if kind else None
        if executor is None:
            executor = await asyncio.to_thread(self._executor_for, claim)
        try:
            await executor.execute(self.runtime, claim)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("executor crashed for queue item %s (run %s)", claim.id, claim.run_id)
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.ctx.queue.complete, claim.id, self.worker_id)
        finally:
            self._wake.set()

    def _executor_for(self, claim: ClaimedItem) -> RunExecutor:
        from agenttape.infrastructure.db.models import Run

        with self.ctx.session() as session:
            run = session.get(Run, claim.run_id) if claim.run_id else None
            kind = run.kind if run is not None else "agent"
        executor = self.executors.get(kind)
        if executor is None:
            raise RuntimeError(f"no executor registered for run kind '{kind}'")
        return executor

    async def claim_once(self) -> ClaimedItem | None:
        return await asyncio.to_thread(
            self.ctx.queue.claim, self.worker_id, self.ctx.settings.lease_seconds
        )

    async def run_until_idle(self, *, max_items: int = 100) -> int:
        """Test/CLI helper: claim and execute items one at a time until none are ready."""
        count = 0
        while count < max_items:
            claim = await self.claim_once()
            if claim is None:
                break
            await self._execute(claim)
            count += 1
        return count

    async def run(self, stop: asyncio.Event) -> None:
        """Main loop until ``stop`` is set. Active attempts are interrupted on shutdown."""
        log.info("worker %s starting (concurrency %d)", self.worker_id, self.concurrency)
        await asyncio.to_thread(self._maintenance)
        await asyncio.to_thread(self._backfill_findings)
        maintenance_every = self.ctx.settings.heartbeat_interval_seconds
        loop = asyncio.get_running_loop()
        next_maintenance = loop.time() + maintenance_every
        try:
            while not stop.is_set():
                self._active = {t for t in self._active if not t.done()}
                claimed = False
                if len(self._active) < self.concurrency:
                    try:
                        claim = await self.claim_once()
                    except Exception:
                        log.exception("queue claim failed")
                        claim = None
                    if claim is not None:
                        task = asyncio.create_task(self._execute(claim))
                        self._active.add(task)
                        claimed = True
                if loop.time() >= next_maintenance:
                    try:
                        await asyncio.to_thread(self._maintenance)
                    except Exception:
                        log.exception("worker maintenance failed")
                    next_maintenance = loop.time() + maintenance_every
                if claimed:
                    continue
                self._wake.clear()
                stop_wait = asyncio.create_task(stop.wait())
                wake_wait = asyncio.create_task(self._wake.wait())
                await asyncio.wait(
                    {stop_wait, wake_wait},
                    timeout=self.poll_interval,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                stop_wait.cancel()
                wake_wait.cancel()
        finally:
            for task in self._active:
                task.cancel()
            for task in self._active:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self._heartbeat, "stopped")
            log.info("worker %s stopped", self.worker_id)
