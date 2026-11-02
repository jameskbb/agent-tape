from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import update

from agenttape.application.context import AppContext
from agenttape.application.services.dashboard import DashboardService
from agenttape.infrastructure.db.models import Run
from tests.conftest import demo_profile, drive, start_run
from tests.integration.test_findings_inbox import _build_window


def test_empty_dashboard_is_honest(ctx: AppContext) -> None:
    d = DashboardService(ctx).summary(days=7)
    assert d.success_rate.rate is None and d.success_rate.finished == 0
    assert d.duration.median_ms is None and d.duration.p90_ms is None
    assert d.usage.total_tokens is None and d.cost.total_usd is None
    assert len(d.activity) == 7 and all(a.succeeded == a.failed == a.other == 0 for a in d.activity)
    assert d.failure_categories == [] and d.recent_failures == []
    # The findings block is honest too - never a guessed value for an empty window.
    assert d.findings.recorded == 0 and d.findings.citations == 0
    assert d.findings.all_quotes_match == 0 and d.findings.declared_only == 0
    assert d.findings.verdicts == {} and d.findings.unchecked == 0


def test_findings_block_shares_the_findings_inbox_read_model(
    ctx: AppContext, repo_project: str
) -> None:
    _build_window(ctx, repo_project)
    d = DashboardService(ctx).summary(days=7)
    assert d.findings.recorded == 3
    assert d.findings.all_quotes_match == 1
    assert d.findings.declared_only == 1
    assert d.findings.verdicts == {"quote_not_found": 1, "quote_matches": 1}
    assert d.findings.unchecked == 0


def test_outcomes_durations_and_simulated_usage(ctx: AppContext) -> None:
    ok = demo_profile(ctx)
    bad = demo_profile(ctx, "fail")
    for _ in range(3):
        drive(ctx, start_run(ctx, ok))
    failed = start_run(ctx, bad)
    drive(ctx, failed)
    cancelled = start_run(ctx, ok)
    from agenttape.application.services.runs import RunService

    RunService(ctx).cancel_run(cancelled)
    queued = start_run(ctx, ok)

    d = DashboardService(ctx).summary()
    assert d.success_rate.succeeded == 3 and d.success_rate.finished == 4
    assert d.success_rate.rate == 0.75  # cancelled runs are excluded
    assert d.duration.sample == 4 and d.duration.median_ms is not None
    today = d.activity[-1]
    assert (today.succeeded, today.failed, today.other) == (3, 1, 1)
    assert [(c.category, c.count) for c in d.failure_categories] == [("validation_failed", 1)]
    assert d.recent_failures[0].id == failed
    assert d.live["queued"] == 1 and d.live_runs[0].id == queued
    # Demo usage is simulated: counted separately, never as real tokens.
    assert (
        d.usage.simulated_runs == 3 and d.usage.total_tokens is None and d.usage.runs_reporting == 0
    )


def test_real_usage_and_cost_are_summed(ctx: AppContext) -> None:
    run_id = start_run(ctx, demo_profile(ctx))
    drive(ctx, run_id)
    with ctx.session() as session:
        session.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(usage={"input_tokens": 100, "output_tokens": 50}, cost={"total_usd": 0.25})
        )
    d = DashboardService(ctx).summary()
    assert (
        d.usage.total_tokens == 150 and d.usage.runs_reporting == 1 and d.usage.simulated_runs == 0
    )
    assert d.cost.total_usd == 0.25 and d.cost.runs_reporting == 1


def test_dashboard_endpoint(client: TestClient) -> None:
    body = client.get("/api/v1/dashboard", params={"days": 30}).json()
    assert body["window_days"] == 30 and len(body["activity"]) == 30
    assert client.get("/api/v1/dashboard", params={"days": 0}).status_code == 422
