"""Worker hook: verification runs once, then automatic search re-runs are queued, when a
run finishes.

Verification runs when a run reaches a terminal status - never during an attempt, never
on an approval pause, never on a page read (computing a
verdict on read would make every page view pay for it). It is idempotent:
``VerificationService.verify_run(trigger="automatic")`` records nothing for a citation
whose recorded inputs have not changed since the last check.

Once verification has run, ``ReproductionService.request_automatic`` queues one automatic
re-run for every cited read-only search that has no reproduction yet - queued
only; the worker executes it later, in its own job, the same way a user's own re-run
request is executed. Also only on terminal statuses, also idempotent (a citation already
holding a reproduction, of any trigger, is skipped), and also never raises.

:mod:`agenttape.context_evidence.hooks` is imported first (for its side effect) so
``context_on_terminal`` - which ingests FINDING events into citations - is registered,
and therefore runs, before :func:`verify_on_terminal`: citations must exist before this
hook tries to verify or re-run them.

A verification or reproduction-queueing bug must never fail or delay a run's
finalization: any exception here is logged with the run id and never re-raised.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import agenttape.context_evidence.hooks  # noqa: F401  (registers context_on_terminal first)
from agenttape.application.services.reproductions import ReproductionService
from agenttape.application.services.verification import VerificationService
from agenttape.domain.enums import TERMINAL_STATUS_VALUES
from agenttape.worker import hooks

if TYPE_CHECKING:
    from agenttape.application.context import AppContext

log = logging.getLogger(__name__)


async def verify_on_terminal(ctx: AppContext, run_id: str, status: str) -> None:
    """Verify ``run_id``'s citations, then queue automatic search re-runs, once it reaches
    a terminal status.

    Returns immediately for any non-terminal status (an approval pause included) - this
    hook fires on every run-terminal transition, but only a real end of the run's
    lifecycle is a verification/reproduction trigger.
    """
    if status not in TERMINAL_STATUS_VALUES:
        return
    try:
        await asyncio.to_thread(VerificationService(ctx).verify_run, run_id, trigger="automatic")
    except Exception:
        log.exception("automatic verification failed for run %s", run_id)
    try:
        await asyncio.to_thread(ReproductionService(ctx).request_automatic, run_id)
    except Exception:
        log.exception("automatic reproduction queueing failed for run %s", run_id)


hooks.register_on_run_terminal(verify_on_terminal)
