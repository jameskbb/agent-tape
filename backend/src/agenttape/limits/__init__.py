"""Budgets and concurrency limits.

:func:`install` registers the worker hooks and the queue claim guard. It is called when
the worker loads its executors (``agenttape/worker/executors/limits.py``), so every
worker - ``agenttape serve``, ``agenttape worker``, inline CLI runs, and tests - enforces
the same limits.
"""

from __future__ import annotations

_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    from agenttape.infrastructure.queue import register_claim_guard
    from agenttape.limits.concurrency import ConcurrencyGuard
    from agenttape.limits.monitor import AFTER_ATTEMPT_ORDER, budget_after_attempt, budget_monitor
    from agenttape.worker import hooks

    hooks.register_attempt_monitor(budget_monitor)
    hooks.register_after_attempt(budget_after_attempt, order=AFTER_ATTEMPT_ORDER)
    register_claim_guard(ConcurrencyGuard())
    _installed = True
