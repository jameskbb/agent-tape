from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agenttape.cli.main import app

runner = CliRunner()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    monkeypatch.setenv("AGENTTAPE_DATA_DIR", str(data))
    monkeypatch.setenv("AGENTTAPE_DEMO_STEP_DELAY_MS", "0")
    monkeypatch.setenv("AGENTTAPE_ALLOWED_ROOTS", str(tmp_path))
    return data


def invoke(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, list(args))
    return result.exit_code, result.output


def test_init_and_doctor(env: Path) -> None:
    code, out = invoke("init")
    assert code == 0 and "Workspace" in out and (env / "agenttape.db").exists()
    code, out = invoke("doctor", "--json")
    checks = {c["check"]: c for c in json.loads(out)}
    assert code == 0
    assert checks["migrations"]["level"] == "ok" and checks["adapter:demo"]["level"] == "ok"
    assert checks["bind"]["level"] == "ok"


def test_demo_runs_inline_and_runs_commands(env: Path, tmp_path: Path) -> None:
    code, out = invoke("demo", "--inline")
    assert code == 0, out
    assert "Run succeeded" in out and "succeeded" in out
    code, out = invoke("runs", "list", "--json")
    page = json.loads(out)
    # Newest first; demo seeds may add earlier runs (the Git compare pair).
    assert page["total"] >= 1 and page["items"][0]["status"] == "succeeded"
    run_id = page["items"][0]["id"]
    code, out = invoke("runs", "show", run_id[:8])
    assert code == 0 and "simulated" in out and "summary.md" in out
    target = tmp_path / "bundle.json"
    code, out = invoke("export", run_id, "-o", str(target))
    assert code == 0 and json.loads(target.read_text())["run"]["id"] == run_id
    code, out = invoke("runs", "cancel", run_id)
    assert code == 1 and "nothing to cancel" in out


def test_run_command_and_failures_exit_nonzero(env: Path) -> None:
    invoke("seed")
    code, out = invoke(
        "run",
        "--profile",
        "Demo Agent (fails checks)",
        "--prompt-template",
        "Improve the greeting service",
        "--input",
        "add_tests=false",
        "--follow",
        "--inline",
    )
    assert code == 2 and "validation_failed" in out
    code, out = invoke("run", "--profile", "nope", "--prompt", "x")
    assert code == 1 and "was not found" in out


def test_seed_and_reset_demo(env: Path) -> None:
    code, out = invoke("seed", "--json")
    assert code == 0 and "AgentTape Demo Project" in json.loads(out)["created"]["project"]
    code, out = invoke("reset-demo", "--yes")
    assert code == 0 and "agent_profiles" in out


def test_token_create_writes_only_a_hash(env: Path) -> None:
    invoke("init")
    code, out = invoke("token", "create")
    token = out.splitlines()[0]
    stored = (env / "api_token.sha256").read_text().strip()
    assert (
        code == 0
        and token.startswith("atk_")
        and token not in stored
        and stored.startswith("sha256:")
    )
    code, _ = invoke("token", "create")
    assert code == 1


def test_serve_refuses_public_bind_without_token(env: Path) -> None:
    code, out = invoke("serve", "--host", "0.0.0.0")
    assert code == 1 and "Refusing to bind" in out


def test_commands_are_registered() -> None:
    _code, out = invoke("--help")
    for command in (
        "init",
        "doctor",
        "serve",
        "worker",
        "demo",
        "run",
        "runs",
        "export",
        "seed",
        "reset-demo",
        "migrate",
    ):
        assert command in out
