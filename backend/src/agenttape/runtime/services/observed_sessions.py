"""Closes observed Claude Code session runs after the idle window.

Modelled on ``runtime/services/workflows.py``: a periodic sweep run inside ``agenttape
serve`` (disableable with ``--without observed-sessions``). There is no generic "session
went idle" hook event, so idle closure is this out-of-band sweep, not
a hook.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from agenttape.application.context import AppContext
from agenttape.application.services.hook_ingest import hook_ingest
from agenttape.application.services.observed_sessions import ObservedSessionService
from agenttape.application.services.verification import VerificationService
from agenttape.runtime.base import BackgroundService, ServiceSpec, register_service
from agenttape.worker import hooks

log = logging.getLogger(__name__)

MIN_INTERVAL_SECONDS = 1.0
MAX_INTERVAL_SECONDS = 60.0


class ObservedSessionSweeper:
    name = "observed-sessions"

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        window = ctx.settings.observed_idle_timeout_seconds
        self.interval = min(MAX_INTERVAL_SECONDS, max(MIN_INTERVAL_SECONDS, window / 4))

    async def sweep_once(self) -> list[tuple[str, str]]:
        # On the hook ingest thread, in order with the hooks and findings already received:
        # a hook queued before the sweep is ingested before its row is judged idle, and
        # drops pending for a row are written onto it before it is closed and verified.
        closed = await asyncio.wrap_future(
            hook_ingest(self.ctx).in_order(ObservedSessionService(self.ctx).sweep_idle)
        )
        for run_id, status in closed:
            await hooks.fire_run_terminal(self.ctx, run_id, status)
        return closed

    async def recheck_outdated(self) -> list[str]:
        """Once per start: re-verify finished hook-read runs whose ``never_read`` an older
        verifier decided (``VerificationService.recheck_outdated_never_read``)."""
        return await asyncio.to_thread(VerificationService(self.ctx).recheck_outdated_never_read)

    async def run(self, stop: asyncio.Event) -> None:
        try:
            rechecked = await self.recheck_outdated()
            if rechecked:
                log.info("re-checked %d run(s) verified by an older verifier", len(rechecked))
        except Exception:
            log.exception("re-checking runs verified by an older verifier failed")
        while not stop.is_set():
            try:
                closed = await self.sweep_once()
                if closed:
                    log.info("observed-sessions sweep closed %d run(s)", len(closed))
            except Exception:
                log.exception("observed-sessions sweep failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.interval)


def _factory(ctx: AppContext) -> BackgroundService:
    return ObservedSessionSweeper(ctx)


register_service(
    ServiceSpec(
        name="observed-sessions",
        factory=_factory,
        description="Closes observed Claude Code session runs after the idle window",
    )
)
