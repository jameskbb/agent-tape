"""Budget math: merging layers, warnings before the limit, and never counting simulated usage."""

from __future__ import annotations

from agenttape.limits.budget import (
    Measures,
    budget_from_snapshot,
    evaluate,
    merge_budgets,
    reported_cost_usd,
    reported_tokens,
)


def statuses(checks: list) -> dict[str, str]:  # type: ignore[type-arg]
    return {c.key: c.status for c in checks}


def test_layers_merge_with_later_layers_winning() -> None:
    b = merge_budgets(
        [
            ("global default", {"max_changed_files": 50, "max_diff_lines": 1000}),
            ("project", {"max_changed_files": 20, "warn_ratio": 0.9}),
            ("agent profile", {"max_total_tokens": 100_000}),
            ("run", {"max_diff_lines": None, "max_changed_files": 5}),
        ]
    )
    assert b.limits == {"max_changed_files": 5.0, "max_total_tokens": 100_000.0}
    assert b.warn_ratio == 0.9
    assert b.sources == {
        "max_changed_files": "run",
        "max_total_tokens": "agent profile",
        "warn_ratio": "project",
    }


def test_invalid_values_are_ignored() -> None:
    b = merge_budgets(
        [
            (
                "run",
                {
                    "max_changed_files": 0,
                    "max_diff_lines": -1,
                    "max_cost_usd": "1",
                    "warn_ratio": 2,
                    "unknown": 5,
                },
            )
        ]
    )
    assert b.limits == {} and b.warn_ratio == 0.8


def test_budget_from_snapshot_reads_project_and_profile() -> None:
    snapshot = {
        "project": {"settings": {"budget": {"max_changed_files": 10}}},
        "agent_profile": {"config": {"budget": {"max_artifact_bytes": 1024}}},
    }
    b = budget_from_snapshot(snapshot, {"max_diff_lines": 30}, {"max_total_tokens": 5})
    assert b.limits == {
        "max_total_tokens": 5.0,
        "max_changed_files": 10.0,
        "max_artifact_bytes": 1024.0,
        "max_diff_lines": 30.0,
    }


def test_ok_warning_exceeded_boundaries() -> None:
    b = merge_budgets([("run", {"max_changed_files": 10})])
    assert statuses(evaluate(b, Measures(changed_files=7))) == {"max_changed_files": "ok"}
    assert statuses(evaluate(b, Measures(changed_files=8))) == {"max_changed_files": "warning"}
    assert statuses(evaluate(b, Measures(changed_files=10))) == {"max_changed_files": "warning"}
    assert statuses(evaluate(b, Measures(changed_files=11))) == {"max_changed_files": "exceeded"}


def test_simulated_usage_never_trips_real_limits() -> None:
    b = merge_budgets([("run", {"max_total_tokens": 10, "max_cost_usd": 0.01})])
    checks = evaluate(
        b, Measures(usage={"input_tokens": 99_999, "output_tokens": 1, "simulated": True})
    )
    assert statuses(checks) == {
        "max_total_tokens": "not_applicable",
        "max_cost_usd": "not_applicable",
    }
    assert all(c.used is None for c in checks)


def test_unreported_usage_is_unknown_not_zero() -> None:
    b = merge_budgets([("run", {"max_total_tokens": 10, "max_cost_usd": 1})])
    checks = evaluate(b, Measures())
    assert statuses(checks) == {"max_total_tokens": "unknown", "max_cost_usd": "unknown"}
    assert all(c.used is None and c.ratio is None for c in checks)


def test_real_usage_and_cost() -> None:
    assert reported_tokens({"input_tokens": 100, "output_tokens": 50}) == (150.0, None)
    assert reported_tokens({"total_tokens": 7, "input_tokens": 1}) == (7.0, None)
    assert reported_tokens({"model": "x"})[0] is None
    assert reported_cost_usd({"usd": 0.25}) == (0.25, None)
    assert reported_cost_usd({"amount": 2, "currency": "usd"}) == (2.0, None)
    assert reported_cost_usd({"amount": 2, "currency": "EUR"})[0] is None
    b = merge_budgets([("run", {"max_total_tokens": 1000, "max_cost_usd": 1.0})])
    checks = evaluate(
        b, Measures(usage={"input_tokens": 900, "output_tokens": 200}, cost={"usd": 0.5})
    )
    assert statuses(checks) == {"max_total_tokens": "exceeded", "max_cost_usd": "ok"}
    exceeded = next(c for c in checks if c.status == "exceeded")
    assert exceeded.describe() == "Tokens: 1100 of 1000 tokens"
