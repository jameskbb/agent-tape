"""Library CLI commands (they call the same services as the API)."""

from __future__ import annotations

import json
import sys
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
    monkeypatch.setenv("AGENTTAPE_ALLOWED_ROOTS", str(tmp_path / "roots"))
    (tmp_path / "roots").mkdir()
    return data


def invoke(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, list(args))
    return result.exit_code, result.output


def test_seeded_prompts_list_show_export_import(env: Path, tmp_path: Path) -> None:
    assert invoke("seed")[0] == 0
    code, out = invoke("prompts", "list", "--json")
    names = {p["name"] for p in json.loads(out)}
    assert {
        "Draft release notes",
        "Review a file for risks",
        "Improve the greeting service",
    } <= names
    code, out = invoke("prompts", "list", "--tag", "writing")
    assert code == 0 and "Draft release notes" in out and "Review a file" not in out
    release = next(
        p
        for p in json.loads(invoke("prompts", "list", "--json")[1])
        if p["name"] == "Draft release notes"
    )
    assert release["latest_version"] == 2
    code, out = invoke("prompts", "show", release["id"])
    assert code == 0 and "v2" in out and "v1" in out
    bundle = tmp_path / "prompts.json"
    assert invoke("prompts", "export", release["id"], "-o", str(bundle))[0] == 0
    code, out = invoke("prompts", "import", str(bundle))
    assert code == 0 and "Draft release notes (imported)" in out
    code, out = invoke("prompts", "import", str(bundle), "--skip-existing")
    assert code == 0 and "skipped" in out
    # seeding twice stays idempotent
    assert invoke("seed")[0] == 0
    assert (
        len(
            [
                p
                for p in json.loads(invoke("prompts", "list", "--json")[1])
                if p["name"] == "Draft release notes"
            ]
        )
        == 1
    )


def test_agents_discover_and_validate(env: Path, tmp_path: Path) -> None:
    code, out = invoke("agents", "discover", "--json")
    adapters = {a["name"]: a for a in json.loads(out)}
    assert set(adapters) >= {"demo", "generic-command", "claude-code", "codex-cli"}
    assert adapters["claude-code"]["status"] == "unavailable"  # tests point it at a missing path
    tool = tmp_path / "tool"
    tool.write_text(f"#!{sys.executable}\nprint(1)\n")
    tool.chmod(0o755)
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"executable": str(tool), "args": ["-v"]}))
    code, out = invoke("agents", "validate", "generic-command", str(cfg))
    assert code == 1 and "allowlist" in out
    assert invoke("executables", "allow", str(tool))[0] == 0
    code, out = invoke("executables", "list")
    assert str(tool) in out
    code, out = invoke("agents", "validate", "generic-command", str(cfg))
    assert code == 0 and "valid" in out
    assert invoke("executables", "remove", str(tool))[0] == 0
    assert invoke("executables", "remove", str(tool))[0] == 1


def test_projects_check_path(env: Path, tmp_path: Path) -> None:
    inside = tmp_path / "roots" / "repo"
    inside.mkdir()
    code, out = invoke("projects", "check-path", str(inside))
    assert code == 0 and "allowed:   yes" in out
    code, out = invoke("projects", "check-path", str(tmp_path))
    assert code == 1 and "Outside" in out
    assert invoke("projects", "list")[0] == 0
    assert invoke("secrets", "list")[0] == 0
