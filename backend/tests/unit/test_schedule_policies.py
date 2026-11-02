"""Misfire and concurrency policy decisions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agenttape.scheduler.policies import (
    MAX_MISSED_ROWS,
    ConcurrencyAction,
    decide_concurrency,
    plan_due,
)

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def ago(seconds: float) -> datetime:
    return NOW - timedelta(seconds=seconds)


def test_on_time_occurrences_fire_under_both_policies() -> None:
    for policy in ("skip", "run_once"):
        plan = plan_due([ago(5)], NOW, policy, grace_seconds=60)
        assert plan.fire == [ago(5)]
        assert plan.missed == []


def test_misfire_skip_records_all_late_occurrences_as_missed() -> None:
    due = [ago(3600), ago(1800), ago(10)]
    plan = plan_due(due, NOW, "skip", grace_seconds=60)
    assert plan.fire == [ago(10)]
    assert plan.missed == [ago(3600), ago(1800)]


def test_misfire_skip_with_nothing_on_time_fires_nothing() -> None:
    plan = plan_due([ago(3600), ago(1800)], NOW, "skip", grace_seconds=60)
    assert plan.fire == []
    assert plan.missed_total == 2


def test_misfire_run_once_coalesces_into_latest() -> None:
    plan = plan_due([ago(3600), ago(1800), ago(900)], NOW, "run_once", grace_seconds=60)
    assert plan.fire == [ago(900)]
    assert plan.missed == [ago(3600), ago(1800)]


def test_misfire_run_once_prefers_on_time_occurrence() -> None:
    plan = plan_due([ago(3600), ago(5)], NOW, "run_once", grace_seconds=60)
    assert plan.fire == [ago(5)]
    assert plan.missed == [ago(3600)]


def test_missed_rows_are_capped_but_counted() -> None:
    due = [ago(10_000 - i) for i in range(500)]
    plan = plan_due(due, NOW, "skip", grace_seconds=60)
    assert plan.missed_total == 500
    assert len(plan.missed) == MAX_MISSED_ROWS


def test_concurrency_start_when_idle() -> None:
    for policy in ("queue", "skip", "cancel_and_replace"):
        assert (
            decide_concurrency(policy, active_runs=0, queued_occurrences=0, max_queued=1)
            == ConcurrencyAction.START
        )


def test_concurrency_skip() -> None:
    assert (
        decide_concurrency("skip", active_runs=1, queued_occurrences=0, max_queued=5)
        == ConcurrencyAction.SKIP
    )


def test_concurrency_replace() -> None:
    assert (
        decide_concurrency("cancel_and_replace", active_runs=1, queued_occurrences=0, max_queued=1)
        == ConcurrencyAction.REPLACE
    )


def test_concurrency_queue_is_bounded_by_max_queued() -> None:
    assert (
        decide_concurrency("queue", active_runs=1, queued_occurrences=0, max_queued=2)
        == ConcurrencyAction.WAIT
    )
    assert (
        decide_concurrency("queue", active_runs=1, queued_occurrences=1, max_queued=2)
        == ConcurrencyAction.WAIT
    )
    assert (
        decide_concurrency("queue", active_runs=1, queued_occurrences=2, max_queued=2)
        == ConcurrencyAction.SKIP
    )
    assert (
        decide_concurrency("queue", active_runs=1, queued_occurrences=0, max_queued=0)
        == ConcurrencyAction.SKIP
    )
    # Earlier occurrences are still waiting: keep FIFO order even if nothing is running.
    assert (
        decide_concurrency("queue", active_runs=0, queued_occurrences=1, max_queued=3)
        == ConcurrencyAction.WAIT
    )
