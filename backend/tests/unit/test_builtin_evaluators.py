"""Each built-in evaluator against hand-built evaluation contexts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agenttape.domain.enums import EvaluationStatus
from agenttape.evaluations.base import EvaluationContext, EvaluatorRegistry

S = EvaluationStatus
REGISTRY = EvaluatorRegistry.load()


def make_ctx(
    *,
    workdir: Path | None = None,
    events: list[dict[str, Any]] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    changes: list[dict[str, Any]] | None = None,
    outputs: dict[str, Any] | None = None,
    duration_ms: int | None = 1000,
    usage: dict[str, Any] | None = None,
    exit_code: int | None = 0,
    extras: dict[str, Any] | None = None,
) -> EvaluationContext:
    async def load_events(types: list[str] | None = None) -> list[dict[str, Any]]:
        return [e for e in events or [] if not types or e["type"] in types]

    async def load_artifacts() -> list[dict[str, Any]]:
        return list(artifacts or [])

    async def load_changes() -> list[dict[str, Any]]:
        return list(changes or [])

    return EvaluationContext(
        run_id="run",
        attempt_id="att",
        step_run_id=None,
        exit_code=exit_code,
        outcome="succeeded",
        outputs=outputs or {},
        workdir=workdir,
        duration_ms=duration_ms,
        usage=usage,
        load_events=load_events,
        load_artifacts=load_artifacts,
        load_file_changes=load_changes,
        extras=extras or {},
    )


async def run(type_: str, config: dict[str, Any], ctx: EvaluationContext) -> Any:
    evaluator = REGISTRY.get(type_)
    assert evaluator.validate_config(config) == [], evaluator.validate_config(config)
    return await evaluator.evaluate(ctx, config)


def test_all_builtin_evaluators_are_registered() -> None:
    expected = {
        "exit_code",
        "command",
        "file_exists",
        "file_absent",
        "git_diff",
        "regex",
        "json_schema",
        "artifact_produced",
        "duration",
        "usage_cost",
        "human_score",
        "agent_judge",
    }
    assert expected <= {e.type for e in REGISTRY.all()}


# --------------------------------------------------------------------------- command


async def test_command_passes_and_fails_on_exit_code(tmp_path: Path) -> None:
    ok = await run("command", {"argv": ["true"]}, make_ctx(workdir=tmp_path))
    assert ok.status == S.PASS
    bad = await run("command", {"argv": ["false"]}, make_ctx(workdir=tmp_path))
    assert bad.status == S.FAIL and bad.evidence["exit_code"] == 1
    expected = await run(
        "command", {"argv": ["false"], "expected_exit_codes": [1]}, make_ctx(workdir=tmp_path)
    )
    assert expected.status == S.PASS


async def test_command_respects_policy_and_timeout(tmp_path: Path) -> None:
    denied = await run("command", {"argv": ["rm", "-rf", "x"]}, make_ctx(workdir=tmp_path))
    assert denied.status == S.ERROR and "never allowed" in denied.explanation
    needs = await run("command", {"argv": ["sleep", "5"]}, make_ctx(workdir=tmp_path))
    assert needs.status == S.ERROR and "allowlist" in needs.explanation
    ctx = make_ctx(workdir=tmp_path, extras={"project_settings": {"command_allowlist": ["sleep"]}})
    slow = await run("command", {"argv": ["sleep", "5"], "timeout_seconds": 0.3}, ctx)
    assert slow.status == S.FAIL and slow.evidence["timed_out"] is True
    none = await run("command", {"argv": ["true"]}, make_ctx(workdir=None))
    assert none.status == S.SKIPPED


def test_command_config_validation() -> None:
    evaluator = REGISTRY.get("command")
    assert evaluator.validate_config({"argv": "ls -la"})
    assert evaluator.validate_config({"argv": ["ls"], "timeout_seconds": 0})


# --------------------------------------------------------------------------- files


async def test_file_exists_and_absent(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x")
    ctx = make_ctx(workdir=tmp_path)
    assert (await run("file_exists", {"paths": ["src/a.py", "src/*.py"]}, ctx)).status == S.PASS
    missing = await run("file_exists", {"paths": ["src/b.py"]}, ctx)
    assert missing.status == S.FAIL and "src/b.py" in missing.explanation
    assert (await run("file_absent", {"paths": ["*.log"]}, ctx)).status == S.PASS
    assert (await run("file_absent", {"paths": ["src/**/*.py"]}, ctx)).status == S.FAIL


def test_file_paths_cannot_escape_the_workdir() -> None:
    evaluator = REGISTRY.get("file_exists")
    assert evaluator.validate_config({"paths": ["../etc/passwd"]})
    assert evaluator.validate_config({"paths": ["/etc/passwd"]})


# --------------------------------------------------------------------------- git diff


async def test_git_diff_skips_honestly_without_data() -> None:
    result = await run("git_diff", {"max_files": 3}, make_ctx())
    assert result.status == S.SKIPPED and "No file-change data" in result.explanation


async def test_git_diff_limits_and_protected_paths() -> None:
    changes = [
        {"path": "src/a.py", "insertions": 10, "deletions": 2, "is_protected": False},
        {"path": ".github/ci.yml", "insertions": 1, "deletions": 0, "is_protected": False},
    ]
    ctx = make_ctx(
        changes=changes, extras={"project_settings": {"protected_paths": [".github/**"]}}
    )
    result = await run("git_diff", {"max_files": 5, "max_lines": 100}, ctx)
    assert result.status == S.FAIL and result.evidence["protected"] == [".github/ci.yml"]
    warn = await run("git_diff", {"protected_is_failure": False}, ctx)
    assert warn.status == S.WARNING
    over = await run("git_diff", {"max_lines": 5, "protected_paths": []}, ctx)
    assert over.status == S.FAIL and "13 lines" in over.explanation
    ok = await run("git_diff", {"max_files": 5, "protected_paths": []}, ctx)
    assert ok.status == S.PASS


async def test_git_diff_counts_captured_empty_change_sets() -> None:
    ctx = make_ctx(events=[{"type": "git_change", "payload": {"total_files": 0}}])
    assert (await run("git_diff", {"max_files": 0}, ctx)).status == S.PASS


# --------------------------------------------------------------------------- regex


async def test_regex_match_and_absence() -> None:
    events = [
        {"type": "stdout", "payload": {"text": "3 passed, 0 failed"}},
        {"type": "stderr", "payload": {"text": "DeprecationWarning: x"}},
    ]
    ctx = make_ctx(events=events)
    assert (await run("regex", {"pattern": r"\d+ passed"}, ctx)).status == S.PASS
    absent = await run("regex", {"pattern": "Traceback", "mode": "absent"}, ctx)
    assert absent.status == S.PASS
    found = await run("regex", {"pattern": "deprecation", "mode": "absent", "flags": "i"}, ctx)
    assert found.status == S.FAIL
    only_stdout = await run("regex", {"pattern": "Deprecation", "source": "stdout"}, ctx)
    assert only_stdout.status == S.FAIL
    outs = make_ctx(outputs={"summary": "Validated names"})
    assert (
        await run("regex", {"pattern": "Validated", "source": "outputs"}, outs)
    ).status == S.PASS


def test_regex_rejects_bad_patterns() -> None:
    evaluator = REGISTRY.get("regex")
    assert evaluator.validate_config({"pattern": "("})
    assert evaluator.validate_config({"pattern": "x" * 2000})
    assert evaluator.validate_config({"pattern": "x", "flags": "q"})


# --------------------------------------------------------------------------- json schema

SCHEMA = {
    "type": "object",
    "required": ["files_changed"],
    "properties": {"files_changed": {"type": "integer", "minimum": 1}},
}


async def test_json_schema_on_outputs_and_paths() -> None:
    ok = await run("json_schema", {"schema": SCHEMA}, make_ctx(outputs={"files_changed": 3}))
    assert ok.status == S.PASS
    bad = await run("json_schema", {"schema": SCHEMA}, make_ctx(outputs={"files_changed": 0}))
    assert bad.status == S.FAIL and bad.evidence["errors"][0]["path"] == "files_changed"
    nested = make_ctx(outputs={"result": {"files_changed": 2}})
    assert (await run("json_schema", {"schema": SCHEMA, "path": "result"}, nested)).status == S.PASS
    missing = await run("json_schema", {"schema": SCHEMA, "path": "nope"}, nested)
    assert missing.status == S.FAIL


async def test_json_schema_on_artifacts() -> None:
    async def reader(artifact_id: str, max_bytes: int) -> str:
        return json.dumps({"files_changed": 5})

    artifacts = [{"id": "a1", "name": "report.json"}]
    ctx = make_ctx(artifacts=artifacts, extras={"read_artifact": reader})
    cfg = {"schema": SCHEMA, "source": "artifact", "artifact_name": "report.json"}
    assert (await run("json_schema", cfg, ctx)).status == S.PASS
    gone = await run("json_schema", {**cfg, "artifact_name": "other.json"}, ctx)
    assert gone.status == S.FAIL


def test_json_schema_rejects_invalid_schemas() -> None:
    assert REGISTRY.get("json_schema").validate_config({"schema": {"type": 5}})


# --------------------------------------------------------------------------- artifacts, duration


async def test_artifact_produced() -> None:
    artifacts = [
        {"id": "1", "name": "report.md", "kind": "report", "size_bytes": 200},
        {"id": "2", "name": "diff.patch", "kind": "diff", "size_bytes": 0},
    ]
    ctx = make_ctx(artifacts=artifacts)
    assert (await run("artifact_produced", {"name": "*.md"}, ctx)).status == S.PASS
    assert (await run("artifact_produced", {"kind": "diff", "min_bytes": 1}, ctx)).status == S.FAIL
    assert (await run("artifact_produced", {"min_count": 2}, ctx)).status == S.PASS


async def test_duration_thresholds() -> None:
    assert (await run("duration", {"max_seconds": 2}, make_ctx(duration_ms=1500))).status == S.PASS
    assert (await run("duration", {"max_seconds": 1}, make_ctx(duration_ms=1500))).status == S.FAIL
    warn = await run("duration", {"warn_seconds": 1}, make_ctx(duration_ms=1500))
    assert warn.status == S.WARNING
    assert (
        await run("duration", {"max_seconds": 1}, make_ctx(duration_ms=None))
    ).status == S.SKIPPED


# --------------------------------------------------------------------------- usage / cost


async def test_usage_is_unavailable_when_not_reported() -> None:
    result = await run("usage_cost", {"max_total_tokens": 100}, make_ctx(usage=None))
    assert result.status == S.SKIPPED and "unavailable" in result.explanation.lower()


async def test_simulated_usage_never_counts_as_real() -> None:
    usage = {"input_tokens": 10**6, "output_tokens": 10**6, "simulated": True}
    result = await run("usage_cost", {"max_total_tokens": 1}, make_ctx(usage=usage))
    assert result.status == S.SKIPPED and "simulated" in result.explanation


async def test_real_usage_and_cost_limits() -> None:
    usage = {"input_tokens": 60, "output_tokens": 50}
    over = await run("usage_cost", {"max_total_tokens": 100}, make_ctx(usage=usage))
    assert over.status == S.FAIL and "110 > 100" in over.explanation
    ok = await run("usage_cost", {"max_input_tokens": 100}, make_ctx(usage=usage))
    assert ok.status == S.PASS
    no_cost = await run("usage_cost", {"max_cost": 1.0}, make_ctx(usage=usage))
    assert no_cost.status == S.SKIPPED
    cost = make_ctx(usage=usage, extras={"cost": {"amount": 2.5, "currency": "USD"}})
    assert (await run("usage_cost", {"max_cost": 1.0}, cost)).status == S.FAIL


# --------------------------------------------------------------------------- human, judge


async def test_human_score_waits_for_a_person() -> None:
    result = await run("human_score", {"max_score": 5}, make_ctx())
    assert result.status == S.SKIPPED and "person" in result.explanation


async def test_agent_judge_is_disabled_without_a_backend() -> None:
    evaluator = REGISTRY.get("agent_judge")
    assert evaluator.availability() is not None  # type: ignore[attr-defined]
    result = await run("agent_judge", {"rubric": "Is the change minimal?"}, make_ctx())
    assert result.status == S.SKIPPED and "disabled" in result.explanation
    assert result.score is None


@pytest.mark.parametrize("type_", ["duration", "usage_cost", "agent_judge", "git_diff"])
def test_configs_are_validated(type_: str) -> None:
    bad = {"duration": {}, "usage_cost": {}, "agent_judge": {}, "git_diff": {"max_files": -1}}
    assert REGISTRY.get(type_).validate_config(bad[type_])


@pytest.mark.parametrize("type_", ["regex", "json_schema"])
async def test_catastrophic_patterns_time_out_without_blocking_the_loop(
    type_: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import time

    from agenttape.evaluations import isolated

    monkeypatch.setattr(isolated, "MATCH_TIMEOUT_SECONDS", 1.0)
    evil = "a" * 40 + "!"
    pattern = r"^(a+)+$"
    if type_ == "regex":
        config: dict[str, Any] = {"pattern": pattern, "source": "stdout"}
        ctx = make_ctx(events=[{"type": "stdout", "payload": {"text": evil}}])
    else:
        config = {"schema": {"type": "string", "pattern": pattern}}
        ctx = make_ctx(outputs={"x": evil})
        config["path"] = "x"
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.05)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    started = time.monotonic()
    with pytest.raises(isolated.MatchTimeoutError):
        await run(type_, config, ctx)
    beat.cancel()
    assert time.monotonic() - started < 5
    assert ticks >= 10  # the event loop kept running while the child matched
