"""CLI parity: ``agenttape runs challenge`` / ``runs challenges`` call the same
``FindingReviewService`` as the API - never a second code path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agenttape.api.app import create_app
from agenttape.application.context import AppContext
from agenttape.application.schemas.catalog import ProjectCreate
from agenttape.application.services.projects import ProjectService
from agenttape.bootstrap import build_context
from agenttape.cli.main import app
from agenttape.config import load_settings
from tests.conftest import demo_profile
from tests.fixtures.review_injection import F_REF, F_UP, finding_event
from tests.integration.test_context_evidence import git, run_with

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


def _seed_target(tmp_path: Path) -> tuple[AppContext, str, str]:
    """Build a context on the same on-disk database the CLI's own contexts open, seed a
    finished target run with F-UP (upheld) and F-REF (refuted), and return the context,
    the target run id, and a Demo Agent reviewer profile id."""
    ctx = build_context(load_settings(), migrate=True)
    repo = tmp_path / "cli-challenge-repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "greeter.py").write_text("def greet():\n    return 'hi'\n")
    git(repo, "init", "--quiet", "--initial-branch=main")
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", "init")
    project_id = (
        ProjectService(ctx).create(ProjectCreate(name="CliChallenge", repo_path=str(repo))).id
    )
    target_run_id = run_with(
        ctx,
        project_id,
        [
            finding_event(F_UP),
            finding_event(F_REF),
        ],
    )
    reviewer_profile_id = demo_profile(ctx, "finding_review")
    return ctx, target_run_id, reviewer_profile_id


def test_cli_challenge_starts_a_challenge_and_reports_findings_reviewed(
    env: Path, tmp_path: Path
) -> None:
    ctx, target_run_id, reviewer_profile_id = _seed_target(tmp_path)
    ctx.close()

    code, out = invoke(
        "runs",
        "challenge",
        target_run_id[:8],
        "--profile",
        reviewer_profile_id,
        "--follow",
        "--inline",
    )
    assert code == 0, out
    assert "Challenge run" in out
    assert "2 finding(s)" in out
    assert "model judgment" in out


def test_cli_challenge_accepts_the_reviewer_profile_by_name(env: Path, tmp_path: Path) -> None:
    """``--profile`` is documented as an id or a name, and resolves a name the
    way ``runs start`` does; an unknown name is a clear not-found error."""
    ctx, target_run_id, reviewer_profile_id = _seed_target(tmp_path)
    ctx.close()

    code, out = invoke(
        "runs", "challenge", target_run_id, "--profile", "demo-finding_review", "--json"
    )
    assert code == 0, out
    assert json.loads(out)["run"]["agent_profile_id"] == reviewer_profile_id

    code, out = invoke("runs", "challenge", target_run_id, "--profile", "No Such Reviewer")
    assert code != 0
    assert "No Such Reviewer" in out


def test_cli_challenges_lists_outcomes_and_json_matches_the_api(env: Path, tmp_path: Path) -> None:
    ctx, target_run_id, reviewer_profile_id = _seed_target(tmp_path)

    code, out = invoke(
        "runs",
        "challenge",
        target_run_id,
        "--profile",
        reviewer_profile_id,
        "--follow",
        "--inline",
    )
    assert code == 0, out

    code, out = invoke("runs", "challenges", target_run_id)
    assert code == 0, out
    assert out.count("(model judgment)") == 2
    assert "unavailable" in out

    code, json_out = invoke("runs", "challenges", target_run_id, "--json")
    assert code == 0, json_out
    cli_payload = json.loads(json_out)

    api_app = create_app(ctx, web_dist=ctx.settings.data_dir / "no-web-dist")
    with TestClient(api_app, base_url="http://127.0.0.1") as client:
        api_payload = client.get(f"/api/v1/runs/{target_run_id}/challenges").json()
    ctx.close()

    assert [c["run"]["id"] for c in cli_payload] == [c["run"]["id"] for c in api_payload]
    assert [c["state"] for c in cli_payload] == [c["state"] for c in api_payload]
    assert [c["outcomes"] for c in cli_payload] == [c["outcomes"] for c in api_payload]


def test_cli_challenge_on_a_run_with_no_findings_exits_1(env: Path, tmp_path: Path) -> None:
    ctx = build_context(load_settings(), migrate=True)
    repo = tmp_path / "cli-empty-repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "greeter.py").write_text("def greet():\n    return 'hi'\n")
    git(repo, "init", "--quiet", "--initial-branch=main")
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", "init")
    project_id = ProjectService(ctx).create(ProjectCreate(name="CliEmpty", repo_path=str(repo))).id
    empty_run_id = run_with(ctx, project_id, [])
    reviewer_profile_id = demo_profile(ctx, "finding_review")
    ctx.close()

    code, out = invoke("runs", "challenge", empty_run_id, "--profile", reviewer_profile_id)
    assert code == 1
    assert "no findings" in out.lower()
