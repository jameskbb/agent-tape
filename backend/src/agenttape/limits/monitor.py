"""Budget enforcement inside the worker.

* :func:`budget_monitor` (an attempt monitor) polls what the attempt has used - reported
  usage and cost events, recorded file changes, stored artifacts - emits one ``warning``
  event per budget when it crosses ``warn_ratio`` (default 80%), warns at 80% of the
  wall-clock timeout and on the final attempt, and stops the attempt with
  ``budget_exceeded`` as soon as a limit is passed.
* :func:`budget_after_attempt` (an after-attempt hook, ordered late so diff capture runs
  first) re-checks with the adapter's final result and turns a success into a structured
  ``budget_exceeded`` failure when a limit was passed; the result's ``outputs.budget``
  records every check.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, or_, select

from agenttape.adapters.base import AdapterResult, EventEmitter, ExecutionContext
from agenttape.domain.enums import EventType, FailureCategory, Severity
from agenttape.domain.usage import merge_usage
from agenttape.infrastructure.db.models import Artifact, Event, FileChange, Run
from agenttape.limits.budget import (
    BudgetCheck,
    EffectiveBudget,
    Measures,
    budget_from_snapshot,
    evaluate,
)
from agenttape.limits.settings import load_limits

if TYPE_CHECKING:
    from agenttape.application.context import AppContext

POLL_SECONDS = 1.0
AFTER_ATTEMPT_ORDER = 90


def effective_budget(ctx: AppContext, run_id: str) -> EffectiveBudget:
    with ctx.session() as session:
        run = session.get(Run, run_id)
        if run is None:
            return EffectiveBudget()
        default = load_limits(session).default_budget
        return budget_from_snapshot(dict(run.snapshot or {}), run.budget, default)


def measure(
    ctx: AppContext,
    run_id: str,
    attempt_id: str | None,
    *,
    final_result: AdapterResult | None = None,
) -> Measures:
    """What the run has used. During an attempt usage/cost come from reported events
    (increments); with ``final_result`` they come from the adapter's final totals."""
    with ctx.session() as session:
        run = session.get(Run, run_id)
        prior_usage = run.usage if run is not None else None
        prior_cost = run.cost if run is not None else None
        if final_result is not None:
            usage = merge_usage(prior_usage, final_result.usage)
            cost = final_result.cost or prior_cost
        else:
            usage = prior_usage
            cost = prior_cost
            rows = session.execute(
                select(Event.usage, Event.cost).where(
                    Event.run_id == run_id,
                    Event.attempt_id == attempt_id,
                    or_(Event.usage.is_not(None), Event.cost.is_not(None)),
                )
            ).all()
            for event_usage, event_cost in rows:
                usage = merge_usage(usage, event_usage)
                if event_cost:
                    cost = merge_usage(cost, event_cost)
        change_filter = [FileChange.run_id == run_id]
        if attempt_id:
            change_filter.append(
                or_(FileChange.attempt_id == attempt_id, FileChange.attempt_id.is_(None))
            )
        files, ins, dels = session.execute(
            select(
                func.count(FileChange.id),
                func.coalesce(func.sum(FileChange.insertions), 0),
                func.coalesce(func.sum(FileChange.deletions), 0),
            ).where(*change_filter)
        ).one()
        artifact_bytes = session.scalar(
            select(func.coalesce(func.sum(Artifact.size_bytes), 0)).where(Artifact.run_id == run_id)
        )
    return Measures(
        usage=usage,
        cost=cost,
        changed_files=int(files or 0),
        diff_lines=int(ins or 0) + int(dels or 0),
        artifact_bytes=int(artifact_bytes or 0),
    )


def _payload(kind: str, checks: list[BudgetCheck], budget: EffectiveBudget) -> dict[str, Any]:
    return {
        "kind": kind,
        "checks": [c.as_dict() for c in checks],
        "warn_ratio": budget.warn_ratio,
        "sources": budget.sources,
    }


async def budget_monitor(ctx: AppContext, exec_ctx: ExecutionContext, system: EventEmitter) -> None:
    run_id, attempt = exec_ctx.run.id, exec_ctx.attempt
    budget = await asyncio.to_thread(effective_budget, ctx, run_id)
    if attempt.max_attempts > 1 and attempt.number == attempt.max_attempts:
        await system.event(
            EventType.WARNING,
            f"Final attempt ({attempt.number} of {attempt.max_attempts}); no automatic retry remains",
            {
                "kind": "attempts_warning",
                "attempt": attempt.number,
                "max_attempts": attempt.max_attempts,
            },
            severity=Severity.NOTICE,
        )
    timeout = exec_ctx.limits.get("timeout_seconds")
    warn_at = (
        float(timeout) * budget.warn_ratio if isinstance(timeout, int | float) and timeout else None
    )
    started = asyncio.get_running_loop().time()
    warned: set[str] = set()
    cancel = exec_ctx.cancel
    while not cancel.cancelled:
        if warn_at is not None and "wall_clock" not in warned:
            elapsed = asyncio.get_running_loop().time() - started
            if elapsed >= warn_at:
                warned.add("wall_clock")
                await system.event(
                    EventType.WARNING,
                    f"{budget.warn_ratio:.0%} of the {float(timeout or 0):g}s wall-clock limit used",
                    {
                        "kind": "budget_warning",
                        "key": "timeout_seconds",
                        "limit": timeout,
                        "used": round(elapsed, 1),
                    },
                    severity=Severity.WARNING,
                )
        if not budget.empty:
            m = await asyncio.to_thread(measure, ctx, run_id, attempt.id)
            checks = evaluate(budget, m)
            exceeded = [c for c in checks if c.status == "exceeded"]
            if exceeded:
                message = "Budget exceeded: " + "; ".join(c.describe() for c in exceeded)
                if not cancel.cancelled:
                    # Record first: once the token is cancelled the executor stops this task.
                    await system.event(
                        EventType.ERROR,
                        message + ". Stopping the agent.",
                        _payload("budget_exceeded", checks, budget),
                        severity=Severity.ERROR,
                    )
                    cancel.cancel(FailureCategory.BUDGET_EXCEEDED, message)
                return
            for c in checks:
                if c.status == "warning" and c.key not in warned:
                    warned.add(c.key)
                    await system.event(
                        EventType.WARNING,
                        f"Budget warning: {c.describe()} ({(c.ratio or 0):.0%})",
                        {"kind": "budget_warning", **c.as_dict()},
                        severity=Severity.WARNING,
                    )
        elif warn_at is None:
            return  # nothing to watch
        if await cancel.sleep(POLL_SECONDS):
            return


async def budget_after_attempt(
    ctx: AppContext, exec_ctx: ExecutionContext, result: AdapterResult
) -> AdapterResult:
    budget = await asyncio.to_thread(effective_budget, ctx, exec_ctx.run.id)
    if budget.empty or result.outcome == "needs_approval":
        return result
    m = await asyncio.to_thread(
        measure, ctx, exec_ctx.run.id, exec_ctx.attempt.id, final_result=result
    )
    checks = evaluate(budget, m)
    outputs = dict(result.outputs or {})
    outputs["budget"] = _payload("budget_checks", checks, budget)
    exceeded = [c for c in checks if c.status == "exceeded"]
    if result.outcome == "failed" and result.failure_category == FailureCategory.BUDGET_EXCEEDED:
        result.outputs = outputs
        return result
    if exceeded and result.outcome == "succeeded":
        # The run's terminal lifecycle event carries this message and failure category;
        # ``outputs.budget`` holds the structured checks.
        message = "Budget exceeded: " + "; ".join(c.describe() for c in exceeded)
        return AdapterResult.failure(
            FailureCategory.BUDGET_EXCEEDED,
            message,
            exit_code=result.exit_code,
            outputs=outputs,
            usage=result.usage,
            cost=result.cost,
        )
    result.outputs = outputs
    return result
