"""Registers the workflow maintenance sweep as a background service.

Every few seconds it re-wakes workflow runs whose queue wake-up was lost (a crash or
restart at the wrong moment) and expires overdue approvals. ``agenttape serve`` starts
it; ``--without workflows`` disables it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from agenttape.application.context import AppContext
from agenttape.runtime.base import BackgroundService, ServiceSpec, register_service
from agenttape.workflows.maintenance import sweep_workflows

log = logging.getLogger("agenttape.workflows")

SWEEP_SECONDS = 2.0


class WorkflowMaintenance:
    name = "workflows"

    def __init__(self, ctx: AppContext, interval: float = SWEEP_SECONDS) -> None:
        self.ctx = ctx
        self.interval = interval

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                report = await asyncio.to_thread(sweep_workflows, self.ctx)
                if report["woken"] or report["expired_approvals"]:
                    log.info("workflow sweep: %s", report)
            except Exception:
                log.exception("workflow sweep failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.interval)


def _factory(ctx: AppContext) -> BackgroundService:
    return WorkflowMaintenance(ctx)


register_service(
    ServiceSpec(
        name="workflows",
        factory=_factory,
        description="Re-wakes stalled workflow runs and expires overdue approvals",
    )
)
