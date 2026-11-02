"""Budgets and concurrency limits enforced by the worker and the queue."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agenttape.adapters.base import AdapterResult, AgentAdapter, DiscoveryResult, ExecutionContext
from agenttape.application.context import AppContext
from agenttape.application.schemas.catalog import AgentProfileCreate
from agenttape.application.schemas.schedules import LimitsSettings
from agenttape.application.services.limits import LimitsService
from agenttape.application.services.profiles import AgentProfileService
from agenttape.application.services.runs import RunService
from agenttape.bootstrap import build_context
from agenttape.config import Settings
from agenttape.domain.enums import AdapterAvailability, EventType, QueueItemStatus
from agenttape.infrastructure.clock import ManualClock
from agenttape.infrastructure.db.models import QueueItem
from agenttape.infrastructure.queue import DEFER_SECONDS
from agenttape.worker.worker import Worker
from tests.conftest import demo_profile, drive, event_types, start_run


class TokenBurner(AgentAdapter):
    """Reports real (non-simulated) usage increments until cancelled."""

    name = "token_burner"
    display_name = "Token burner (test)"
    capabilities = frozenset({"usage"})

    def discover(self) -> DiscoveryResult:
        return DiscoveryResult(adapter=self.name, status=AdapterAvailability.READY)

    def validate_profile(self, config: dict[str, Any]) -> list[Any]:
        return []

    async def execute(self, ctx: ExecutionContext) -> AdapterResult:
        for _ in range(100):
            await ctx.events.event(
                EventType.USAGE,
                "usage",
                {"input_tokens": 100},
                usage={"input_tokens": 100, "output_tokens": 0},
            )
            if await ctx.cancel.sleep(0.2):
                break
        return AdapterResult.success(usage={"input_tokens": 30_000, "output_tokens": 0})


def events_of(ctx: AppContext, run_id: str, type_: str) -> list[Any]:
    return [e for e in RunService(ctx).list_events(run_id, limit=1000).items if e.type == type_]


# --------------------------------------------------------------------------- budgets


def test_changed_files_budget_fails_with_budget_exceeded(ctx: AppContext) -> None:
    pid = demo_profile(ctx, budget={"max_changed_files": 2})
    detail = drive(ctx, start_run(ctx, pid))
    assert detail.run.status == "failed"
    assert detail.run.failure_category == "budget_exceeded"
    assert "Changed files" in (detail.run.failure_message or "")
    assert (detail.outputs or {}).get("budget") or any(
        e.payload.get("kind") == "budget_exceeded" for e in events_of(ctx, detail.run.id, "error")
    )
    limits = LimitsService(ctx).for_run(detail.run.id)
    check = next(c for c in limits.checks if c.key == "max_changed_files")
    assert check.status == "exceeded" and check.used == 3


def test_budget_exceeded_is_not_retried(ctx: AppContext) -> None:
    pid = demo_profile(ctx, budget={"max_diff_lines": 1}, max_attempts=3)
    detail = drive(ctx, start_run(ctx, pid))
    assert detail.run.failure_category == "budget_exceeded"
    assert len(detail.attempts) == 1


def test_simulated_demo_usage_never_trips_token_budget(ctx: AppContext) -> None:
    pid = demo_profile(ctx, budget={"max_total_tokens": 1, "max_cost_usd": 0.0001})
    detail = drive(ctx, start_run(ctx, pid))
    assert detail.run.status == "succeeded"
    checks = {c.key: c for c in LimitsService(ctx).for_run(detail.run.id).checks}
    assert checks["max_total_tokens"].status == "not_applicable"
    assert checks["max_total_tokens"].used is None
    assert checks["max_cost_usd"].status == "not_applicable"


def test_real_usage_warns_then_stops_the_attempt(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agenttape.limits import monitor

    monkeypatch.setattr(monitor, "POLL_SECONDS", 0.05)
    ctx.adapters.register(TokenBurner())
    pid = (
        AgentProfileService(ctx)
        .create(
            AgentProfileCreate(
                name="burner",
                adapter_type="token_burner",
                config={"budget": {"max_total_tokens": 1500}},
            )
        )
        .id
    )
    detail = drive(ctx, start_run(ctx, pid), timeout=30)
    assert detail.run.status == "failed"
    assert detail.run.failure_category == "budget_exceeded"
    warnings = [
        e
        for e in events_of(ctx, detail.run.id, "warning")
        if e.payload.get("kind") == "budget_warning"
    ]
    assert warnings and warnings[0].payload["key"] == "max_total_tokens"
    errors = [
        e
        for e in events_of(ctx, detail.run.id, "error")
        if e.payload.get("kind") == "budget_exceeded"
    ]
    assert len(errors) == 1
    # The warning came before the stop.
    assert warnings[0].sequence < errors[0].sequence


def test_final_attempt_warning(ctx: AppContext) -> None:
    pid = demo_profile(ctx, "fail_then_retry", max_attempts=2)
    detail = drive(ctx, start_run(ctx, pid))
    assert detail.run.status == "succeeded"
    notes = [
        e
        for e in events_of(ctx, detail.run.id, "warning")
        if e.payload.get("kind") == "attempts_warning"
    ]
    assert len(notes) == 1 and notes[0].payload["attempt"] == 2


def test_global_default_budget_applies(ctx: AppContext) -> None:
    LimitsService(ctx).update(LimitsSettings(default_budget={"max_artifact_bytes": 10}))
    pid = demo_profile(ctx)
    detail = drive(ctx, start_run(ctx, pid))
    assert detail.run.failure_category == "budget_exceeded"
    assert "Artifact storage" in (detail.run.failure_message or "")


def test_limits_api(client: TestClient, ctx: AppContext) -> None:
    got = client.get("/api/v1/settings/limits").json()
    assert got["settings"]["max_concurrent_runs"] is None
    assert {k["key"] for k in got["budget_keys"]} >= {"max_total_tokens", "max_changed_files"}
    put = client.put(
        "/api/v1/settings/limits",
        json={
            "max_concurrent_runs": 3,
            "per_adapter": {"demo": 1},
            "default_budget": {"max_diff_lines": 500},
        },
    )
    assert put.status_code == 200
    assert put.json()["settings"]["per_adapter"] == {"demo": 1}
    bad = client.put("/api/v1/settings/limits", json={"default_budget": {"max_bananas": 1}})
    assert bad.status_code == 422
    rid = start_run(ctx, demo_profile(ctx))
    run_limits = client.get(f"/api/v1/runs/{rid}/limits").json()
    assert run_limits["budget"]["max_diff_lines"] == 500
    assert run_limits["budget_sources"]["max_diff_lines"] == "global default"
    assert {c["scope"] for c in run_limits["concurrency"]} == {"global", "adapter"}
    assert run_limits["retry"]["retry_on"]


# --------------------------------------------------------------------------- concurrency


@pytest.fixture
def mctx(settings: Settings) -> Iterator[AppContext]:
    context = build_context(
        settings, clock=ManualClock(datetime(2026, 5, 1, tzinfo=UTC)), migrate=True
    )
    yield context
    context.close()


def leased(ctx: AppContext) -> int:
    with ctx.session() as session:
        return len(
            session.scalars(
                select(QueueItem).where(QueueItem.status == QueueItemStatus.LEASED.value)
            ).all()
        )


def test_adapter_limit_defers_claims_until_a_slot_frees(mctx: AppContext) -> None:
    Worker(mctx)  # loads executors, which installs the limit hooks
    LimitsService(mctx).update(LimitsSettings(per_adapter={"demo": 1}))
    pid = demo_profile(mctx)
    runs = {start_run(mctx, pid), start_run(mctx, pid)}
    a = mctx.queue.claim("w1", 30)
    assert a is not None and a.run_id in runs
    (second,) = runs - {a.run_id}
    assert mctx.queue.claim("w2", 30) is None
    assert leased(mctx) == 1
    notices = [
        e
        for e in events_of(mctx, second, "warning")
        if e.payload.get("kind") == "concurrency_limit"
    ]
    assert len(notices) == 1 and "Adapter 'demo'" in notices[0].summary
    # Deferred items wait briefly; the notice is not repeated.
    assert isinstance(mctx.clock, ManualClock)
    mctx.clock.advance(2)
    assert mctx.queue.claim("w2", 30) is None
    assert (
        len(
            [
                e
                for e in events_of(mctx, second, "warning")
                if e.payload.get("kind") == "concurrency_limit"
            ]
        )
        == 1
    )
    mctx.queue.complete(a.id, "w1")
    mctx.clock.advance(2)
    b = mctx.queue.claim("w2", 30)
    assert b is not None and b.run_id == second


def test_global_limit_counts_every_adapter(mctx: AppContext) -> None:
    Worker(mctx)
    LimitsService(mctx).update(LimitsSettings(max_concurrent_runs=2))
    pid = demo_profile(mctx)
    for _ in range(4):
        start_run(mctx, pid)
    claims = [mctx.queue.claim(f"w{i}", 30) for i in range(4)]
    assert sum(c is not None for c in claims) == 2
    assert leased(mctx) == 2


def test_project_limit(mctx: AppContext, tmp_path: Any) -> None:
    from agenttape.application.schemas.catalog import ProjectCreate, ProjectSettings
    from agenttape.application.services.projects import ProjectService

    Worker(mctx)
    repo = tmp_path / "repos" / "proj"
    repo.mkdir(parents=True)
    project = ProjectService(mctx).create(
        ProjectCreate(
            name="p", repo_path=str(repo), settings=ProjectSettings(max_concurrent_runs=1)
        )
    )
    pid = demo_profile(mctx)
    in_project = [start_run(mctx, pid, project_id=project.id) for _ in range(2)]
    outside = start_run(mctx, pid)
    got = {c.run_id for c in (mctx.queue.claim(f"w{i}", 30) for i in range(3)) if c is not None}
    assert outside in got
    assert len(got & set(in_project)) == 1


def test_racing_workers_never_exceed_the_limit(settings: Settings) -> None:
    clock = ManualClock(datetime(2026, 5, 1, tzinfo=UTC))
    ctx_a = build_context(settings, clock=clock, migrate=True)
    ctx_b = build_context(settings, clock=clock)
    try:
        Worker(ctx_a)
        LimitsService(ctx_a).update(LimitsSettings(max_concurrent_runs=1))
        pid = demo_profile(ctx_a)
        for _ in range(6):
            start_run(ctx_a, pid)
        outcomes: list[tuple[str, Any]] = []
        barrier = threading.Barrier(4)

        def claim(c: AppContext, name: str) -> None:
            try:
                barrier.wait()
                outcomes.append(("ok", c.queue.claim(name, 30)))
            except BaseException as exc:  # a crashed claimer must fail the test, not vanish
                outcomes.append(("error", exc))

        threads = [
            threading.Thread(target=claim, args=(c, f"w{i}"))
            for i, c in enumerate([ctx_a, ctx_b, ctx_a, ctx_b])
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        errors = [o for kind, o in outcomes if kind == "error"]
        assert errors == [], errors
        assert len(outcomes) == 4
        winners = [o for _kind, o in outcomes if o is not None]
        # Safety: never more than the limit, even with four claimers racing.
        assert len(winners) <= 1, winners
        assert leased(ctx_a) == len(winners)
        # Liveness: the slot is usable. Racers that defer each other may defer every item,
        # so wait out the longest deferral first. Then, if the racers deferred each other, a
        # fresh claim takes the slot; if one racer won, it is refused because the slot is full.
        clock.advance(DEFER_SECONDS * 1.5 + 0.1)
        follow_up = ctx_b.queue.claim("w-late", 30)
        assert (follow_up is not None) == (len(winners) == 0)
        assert leased(ctx_a) == 1
    finally:
        ctx_a.close()
        ctx_b.close()


def test_worker_respects_limit_end_to_end(ctx: AppContext) -> None:
    """Two runs, one slot: the worker runs them one after the other, both succeed."""
    LimitsService(ctx).update(LimitsSettings(per_adapter={"demo": 1}))
    pid = demo_profile(ctx)
    ids = [start_run(ctx, pid) for _ in range(2)]
    worker = Worker(ctx, worker_id="w", concurrency=2, poll_interval=0.05)

    async def main() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run(stop))
        max_seen = 0
        for _ in range(600):
            statuses = [
                await asyncio.to_thread(lambda i=i: RunService(ctx).get_run(i).status) for i in ids
            ]
            # Admitted runs execute; a deferred claim is briefly "leased" before the guard
            # puts it back, so count executing runs rather than raw leases.
            max_seen = max(max_seen, sum(s in ("preparing", "running") for s in statuses))
            if all(s == "succeeded" for s in statuses):
                break
            await asyncio.sleep(0.05)
        stop.set()
        await task
        assert max_seen <= 1

    asyncio.run(main())
    assert [RunService(ctx).get_run(i).status for i in ids] == ["succeeded", "succeeded"]
    assert "warning" in event_types(ctx, ids[1]) or "warning" in event_types(ctx, ids[0])
