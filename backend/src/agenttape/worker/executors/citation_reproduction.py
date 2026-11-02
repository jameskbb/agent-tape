"""Runs a queued citation reproduction.

A queue job, not a run: dispatched by ``claim.payload["run_kind"]`` (``Worker._execute``
checks that before falling back to ``Run.kind``), enqueued with ``run_id=None`` so run
concurrency limits and run-queue reconciliation never treat it as a run. An interrupted job
(worker crash, process kill) leaves a visible ``"running"`` row behind rather than silently
retrying it - a fresh user request for the same citation creates a new row instead.
"""

from __future__ import annotations

import asyncio

from agenttape.application.ports import ClaimedItem
from agenttape.application.services.reproductions import ReproductionService
from agenttape.findings.reproduce import REPRODUCTION_KIND
from agenttape.worker.registry import RunExecutor, WorkerRuntime, register_executor


@register_executor
class CitationReproductionExecutor(RunExecutor):
    kind = REPRODUCTION_KIND

    async def execute(self, runtime: WorkerRuntime, claim: ClaimedItem) -> None:
        try:
            reproduction_id = str(claim.payload.get("reproduction_id") or "")
            await ReproductionService(runtime.ctx).execute(reproduction_id)
        finally:
            await asyncio.to_thread(runtime.ctx.queue.complete, claim.id, runtime.worker_id)
