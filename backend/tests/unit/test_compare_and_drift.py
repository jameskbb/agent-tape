"""Comparison helpers and replay drift detection (pure functions)."""

from __future__ import annotations

from agenttape.application.services.comparison import (
    count_diff,
    dict_diff,
    named_diff,
    numeric_delta,
    usage_diff,
    usage_status,
)
from agenttape.application.services.lineage import compute_drift


def test_usage_status_is_honest() -> None:
    assert usage_status(None) == "unavailable"
    assert usage_status({}) == "unavailable"
    assert usage_status({"simulated": True}) == "unavailable"
    assert usage_status({"input_tokens": 10, "simulated": True}) == "simulated"
    assert usage_status({"input_tokens": 10, "output_tokens": 2}) == "reported"


def test_usage_diff_never_invents_numbers() -> None:
    diff = usage_diff({"input_tokens": 100, "output_tokens": 5, "simulated": True}, None)
    assert diff["a_status"] == "simulated" and diff["b_status"] == "unavailable"
    rows = {r["key"]: r for r in diff["rows"]}
    assert rows["input_tokens"] == {"key": "input_tokens", "a": 100, "b": None, "delta": None}
    both = usage_diff({"input_tokens": 100}, {"input_tokens": 130})
    assert both["rows"][0]["delta"] == 30
    assert usage_diff(None, None)["rows"] == []


def test_numeric_delta() -> None:
    assert numeric_delta(1000, 1500) == 500
    assert numeric_delta(None, 5) is None
    assert numeric_delta(5, None) is None


def test_event_type_counts() -> None:
    rows = count_diff({"stdout": 3, "tool_call": 2}, {"stdout": 5, "warning": 1})
    by_key = {r["key"]: r for r in rows}
    assert by_key["stdout"]["delta"] == 2
    assert by_key["tool_call"] == {"key": "tool_call", "a": 2, "b": 0, "delta": -2}
    assert by_key["warning"]["a"] == 0 and by_key["warning"]["b"] == 1
    assert rows[0]["key"] == "stdout"  # most frequent first


def test_dict_and_named_diffs() -> None:
    assert dict_diff({"goal": "a", "same": 1}, {"goal": "b", "same": 1, "new": True}) == [
        {"key": "goal", "a": "a", "b": "b"},
        {"key": "new", "a": None, "b": True},
    ]
    evals = named_diff(
        [{"name": "Exit code", "status": "pass"}, {"name": "Lint", "status": "fail"}],
        [{"name": "Exit code", "status": "pass"}, {"name": "Tests", "status": "pass"}],
        key="name",
        fields=("status",),
    )
    by_name = {e["name"]: e for e in evals}
    assert by_name["Exit code"]["changed"] is False
    assert by_name["Lint"]["in_b"] is False and by_name["Lint"]["changed"]
    assert by_name["Tests"]["in_a"] is False and by_name["Tests"]["b_status"] == "pass"


SNAPSHOT = {
    "project": {"name": "App", "settings": {"git_isolation": True, "protected_paths": []}},
    "git": {"is_repo": True, "head": "aaa", "dirty": False, "dirty_count": 0, "dirty_files": []},
    "environment": {"agenttape_version": "0.1.0", "python": "3.12.1", "platform": "Linux"},
}
ENV = {"agenttape_version": "0.1.0", "python": "3.12.1", "platform": "Linux"}


def _drift(**kw: object) -> list[str]:
    args: dict[str, object] = {
        "current_git": {"is_repo": True, "head": "aaa", "dirty": False, "dirty_files": []},
        "current_project_settings": {"git_isolation": True, "protected_paths": []},
        "project_exists": True,
        "original_adapter_version": "1.0.0",
        "current_adapter_version": "1.0.0",
        "current_environment": ENV,
    }
    args.update(kw)
    return [d.field for d in compute_drift(SNAPSHOT, **args)]  # type: ignore[arg-type]


def test_no_drift_when_everything_matches() -> None:
    assert _drift() == []


def test_head_and_dirty_drift() -> None:
    assert _drift(current_git={"is_repo": True, "head": "bbb", "dirty": False}) == ["git.head"]
    dirty = {"is_repo": True, "head": "aaa", "dirty": True, "dirty_count": 2}
    assert _drift(current_git=dirty) == ["git.dirty"]


def test_settings_adapter_and_environment_drift() -> None:
    drift = compute_drift(
        SNAPSHOT,
        current_git={"is_repo": True, "head": "aaa", "dirty": False},
        current_project_settings={"git_isolation": False, "protected_paths": []},
        project_exists=True,
        original_adapter_version="1.0.0",
        current_adapter_version="1.1.0",
        current_environment={**ENV, "python": "3.13.0"},
    )
    fields = {d.field: d for d in drift}
    assert set(fields) == {"project.settings", "adapter.version", "environment.python"}
    assert "git_isolation" in fields["project.settings"].message
    assert fields["adapter.version"].current == "1.1.0"


def test_missing_project_and_unknown_versions() -> None:
    assert _drift(project_exists=False, current_git=None, current_project_settings=None) == [
        "project"
    ]
    # An adapter version that was never recorded is not reported as drift.
    assert _drift(original_adapter_version=None, current_adapter_version="9") == []
