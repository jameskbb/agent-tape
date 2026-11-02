"""The demo story's run history: real runs, executed by the real engine.

Adds, on the core demo project:

* a clean success;
* a flaky run whose first attempt fails (``adapter_error``) and whose automatic retry
  succeeds;
* a run whose project checks fail (``validation_failed``, not retried);
* a run stopped by its 6 second wall-clock limit (``timed_out``).

Every demo run still ``queued`` at this point (these, the compare pair, and the "Safe
Code Change" workflow run) is then driven in-process until it finishes or pauses for
approval, so a fresh ``agenttape seed`` leaves a coherent history: completed runs with
real events, a retry, a clear failure, a timeout, two related runs to compare, and one
workflow waiting on approval. Idempotency keys make re-seeding a no-op.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time

from sqlalchemy import select

from agenttape.application.context import AppContext
from agenttape.application.schemas.runs import RunCreate
from agenttape.application.services.runs import RunService
from agenttape.domain.enums import TERMINAL_STATUSES, RunSource, RunStatus
from agenttape.domain.errors import AgentTapeError
from agenttape.infrastructure.db.models import Run
from agenttape.seeds.base import SeedReport
from agenttape.seeds.core_demo import IMPROVE_PROMPT

ORDER = 70
#: Pace of seeded Demo Agent runs (ms between steps): fast, but with visible durations.
SEED_STEP_DELAY_MS = 60
SEED_TIMEOUT_SECONDS = 180

#: (idempotency key, scenario, title, goal)
STORY: list[tuple[str, str, str, str]] = [
    (
        "demo-story:success",
        "success",
        "Validate greeting names",
        "Validate the name argument and reject empty names.",
    ),
    (
        "demo-story:flaky",
        "fail_then_retry",
        "Tighten greeting validation (flaky network)",
        "Strip whitespace before validating the name.",
    ),
    (
        "demo-story:checks-fail",
        "fail",
        "Refactor the greeter (checks fail)",
        "Refactor greet() into smaller helpers.",
    ),
    (
        "demo-story:timeout",
        "timeout",
        "Analyse the whole repository (times out)",
        "Analyse every file and summarise the architecture.",
    ),
]


def _can_execute_inline() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return True
    return False


def settle_demo_runs(ctx: AppContext) -> None:
    """Execute queued demo runs in this process until each finishes or waits (bounded)."""
    from agenttape.worker.worker import Worker

    fast = dataclasses.replace(
        ctx, settings=ctx.settings.model_copy(update={"demo_step_delay_ms": SEED_STEP_DELAY_MS})
    )
    worker = Worker(fast, worker_id="demo-seed", concurrency=1)
    deadline = time.monotonic() + SEED_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        asyncio.run(worker.run_until_idle())
        pending = _unsettled(ctx)
        if not pending:
            return
        time.sleep(0.1)
    pending = _unsettled(ctx)
    if pending:
        # Fail loudly: a half-finished demo story is worse than a clear error.
        raise SeedIncompleteError(
            f"{len(pending)} demo run(s) did not finish within {SEED_TIMEOUT_SECONDS:g}s; "
            "the demo story is incomplete. Run `agenttape reset-demo --yes` and seed again, "
            "or start a worker to finish them.",
            details={"pending": [{"run_id": rid, "status": st} for rid, st in pending]},
        )


class SeedIncompleteError(AgentTapeError):
    code = "seed_incomplete"
    http_status = 503


def _unsettled(ctx: AppContext) -> list[tuple[str, str]]:
    with ctx.session() as session:
        rows = session.execute(
            select(Run.id, Run.status).where(
                Run.is_demo.is_(True),
                Run.status.not_in(
                    [s.value for s in TERMINAL_STATUSES] + [RunStatus.WAITING_FOR_APPROVAL.value]
                ),
            )
        ).all()
    return [(str(r[0]), str(r[1])) for r in rows]


def seed(ctx: AppContext, report: SeedReport) -> None:
    profiles = report.data.get("profiles") or {}
    prompts = report.data.get("prompts") or {}
    project_id = report.data.get("project_id")
    prompt_id = prompts.get(IMPROVE_PROMPT)
    if not report.include_runs or prompt_id is None or project_id is None:
        return
    service = RunService(ctx)
    for key, scenario, title, goal in STORY:
        profile_id = profiles.get(scenario)
        if profile_id is None:
            continue
        result = service.create_run(
            RunCreate(
                title=title,
                agent_profile_id=profile_id,
                prompt_template_id=prompt_id,
                project_id=project_id,
                inputs={"goal": goal},
                source=RunSource.DEMO,
                idempotency_key=key,
                tags=["demo", "story"],
                is_demo=True,
            ),
            actor="demo seed",
        )
        report.add("run", title, created=result.created)
    if report.execute_inline and _can_execute_inline():
        settle_demo_runs(ctx)
