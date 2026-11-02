"""CLI: runs replay/fork/compare, git status/commit/cleanup, retention, export --zip."""

from __future__ import annotations

import json
import zipfile
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


def test_seeded_compare_pair_through_the_cli(env: Path, tmp_path: Path) -> None:
    code, out = invoke("seed", "--json")
    assert code == 0, out
    code, out = invoke("runs", "list", "--json")
    runs = {r["title"]: r for r in json.loads(out)["items"]}
    parent = runs["Improve the greeting service (baseline)"]
    fork = runs["Improve the greeting service (fork: stricter goal)"]
    assert parent["status"] == fork["status"] == "succeeded"

    code, out = invoke("runs", "compare", parent["id"][:8], fork["id"][:8])
    assert code == 0, out
    assert "Relation: b is fork of a" in out and "Inputs that differ" in out and "goal" in out
    assert "simulated" in out
    code, out = invoke("runs", "compare", parent["id"], fork["id"], "--json")
    assert json.loads(out)["relation"] == "b_is_fork_of_a"

    code, out = invoke("git", "status", parent["id"][:8])
    assert code == 0, out
    assert "Worktree" in out and "PROTECTED" in out and "CHANGELOG.md" in out
    assert "validation  pass" in out

    code, out = invoke("git", "cleanup", parent["id"])
    assert code == 1 and "uncommitted" in out

    code, out = invoke("git", "commit", parent["id"], "-m", "Validate names")
    assert code == 0 and "pending" in out

    code, out = invoke("runs", "replay", parent["id"], "--follow", "--inline")
    assert code == 0, out
    assert "Replay run" in out and "Run succeeded" in out

    code, out = invoke("runs", "fork", parent["id"], "-i", "add_tests=false", "--title", "No tests")
    assert code == 0 and "Fork run" in out and "inputs.add_tests" in out

    target = tmp_path / "bundle.zip"
    code, out = invoke("export", parent["id"], "--zip", "-o", str(target))
    assert code == 0, out
    with zipfile.ZipFile(target) as zf:
        names = zf.namelist()
    assert "bundle.json" in names and any(n.endswith("changes.diff") for n in names)
    code, out = invoke("export", parent["id"], "--zip")
    assert code == 1 and "--output" in out


def test_retention_cli(env: Path) -> None:
    code, out = invoke("init")
    assert code == 0
    code, out = invoke("retention", "set", "--max-age-days", "7", "--exclude-demo")
    assert code == 0 and json.loads(out)["max_age_days"] == 7
    code, out = invoke("retention", "show")
    assert json.loads(out)["include_demo"] is False
    code, out = invoke("retention", "preview")
    assert code == 0 and "0 run(s)" in out
    code, out = invoke("retention", "apply", "--yes")
    assert code == 0 and "Nothing to clean up" in out
