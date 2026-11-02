"""Regression tests: process adapters obey the V1 command policy.

Reproduces the reviewer's case (git allowlisted as an executable, a generic-command
profile with ``args=["push", "origin", "master"]``) against a temporary bare remote, plus
the bypass variants: ``-c alias.x=push``, ``-C``, repository aliases, shell aliases,
``git-<sub>`` helper binaries, GIT_* environment injection, and preflight/validation
commands.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agenttape.application.context import AppContext
from agenttape.application.schemas.catalog import AgentProfileCreate, ProjectCreate
from agenttape.application.services.approvals import ApprovalService
from agenttape.application.services.profiles import AgentProfileService
from agenttape.application.services.projects import ProjectService
from agenttape.domain.errors import ValidationError
from agenttape.security import executables
from tests.conftest import drive, event_types, start_run

GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(GIT is None, reason="git is required")

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/nonexistent",
}


def git(*args: str, cwd: Path | None = None) -> str:
    assert GIT is not None
    return subprocess.run(
        [GIT, *args], cwd=cwd, check=True, capture_output=True, text=True, env=GIT_ENV
    ).stdout


@pytest.fixture
def repo(ctx: AppContext) -> dict[str, Any]:
    """A project repo with one commit and a bare 'origin' remote that has not seen it."""
    root = ctx.settings.allowed_roots[0]
    remote = root / "remote.git"
    work = root / "work"
    git("init", "--quiet", "--bare", "--initial-branch=master", str(remote))
    git("init", "--quiet", "--initial-branch=master", str(work))
    (work / "a.txt").write_text("hello\n")
    git("add", "a.txt", cwd=work)
    git("commit", "--quiet", "-m", "seed commit", cwd=work)
    git("remote", "add", "origin", str(remote), cwd=work)
    project = ProjectService(ctx).create(ProjectCreate(name="Repo", repo_path=str(work)))
    executables.save_entries(ctx.settings.data_dir, [str(GIT)])
    return {"work": work, "remote": remote, "project_id": project.id}


def remote_has_commits(remote: Path) -> bool:
    assert GIT is not None
    res = subprocess.run(
        [GIT, "--git-dir", str(remote), "rev-parse", "--verify", "--quiet", "master"],
        capture_output=True,
        env=GIT_ENV,
        check=False,
    )
    return res.returncode == 0


def git_profile(ctx: AppContext, args: list[str], name: str = "git", **extra: Any) -> str:
    config: dict[str, Any] = {
        "executable": str(GIT),
        "args": args,
        "prompt_mode": "none",
        **extra,
    }
    return (
        AgentProfileService(ctx)
        .create(AgentProfileCreate(name=name, adapter_type="generic-command", config=config))
        .id
    )


def run_in(ctx: AppContext, profile_id: str, repo: dict[str, Any]) -> Any:
    return drive(ctx, start_run(ctx, profile_id, project_id=repo["project_id"]))


def assert_refused(ctx: AppContext, detail: Any, repo: dict[str, Any]) -> None:
    assert detail.run.status == "failed", detail.run.status
    assert detail.run.failure_category == "policy_violation", detail.run.failure_message
    assert "command" not in event_types(ctx, detail.run.id)  # nothing was launched
    assert not remote_has_commits(repo["remote"])


def test_reviewer_repro_git_push_is_refused(ctx: AppContext, repo: dict[str, Any]) -> None:
    detail = run_in(ctx, git_profile(ctx, ["push", "origin", "master"]), repo)
    assert_refused(ctx, detail, repo)
    assert "Pushing to a remote is never automated" in (detail.run.failure_message or "")


@pytest.mark.parametrize(
    "args",
    [
        ["-c", "alias.p=push", "p", "origin", "master"],
        ["-calias.p=push", "p", "origin", "master"],
        ["--config-env=alias.p=HOME", "p"],
        ["-C", ".", "push", "origin", "master"],
        ["--git-dir=.git", "push", "origin", "master"],
        ["push", "-fu", "origin", "master"],
        ["send-pack", "origin", "master"],
        ["--exec-path=/tmp", "status"],
        ["reset", "--hard", "HEAD~1"],
        ["branch", "-D", "master"],
        ["branch", "-dr", "origin/master"],
        ["clean", "-fdx"],
        ["merge", "origin/master"],
        ["tag", "-d", "v1"],
        ["checkout", "--", "."],
    ],
)
def test_bypass_variants_are_refused(
    ctx: AppContext, repo: dict[str, Any], args: list[str]
) -> None:
    detail = run_in(ctx, git_profile(ctx, args), repo)
    assert_refused(ctx, detail, repo)


def test_repository_alias_to_push_is_resolved_and_refused(
    ctx: AppContext, repo: dict[str, Any]
) -> None:
    git("config", "alias.ship", "push origin master", cwd=repo["work"])
    detail = run_in(ctx, git_profile(ctx, ["ship"]), repo)
    assert_refused(ctx, detail, repo)


def test_shell_alias_is_refused(ctx: AppContext, repo: dict[str, Any]) -> None:
    git("config", "alias.sneaky", "!git push origin master", cwd=repo["work"])
    detail = run_in(ctx, git_profile(ctx, ["sneaky"]), repo)
    assert_refused(ctx, detail, repo)
    assert "shell command" in (detail.run.failure_message or "")


def test_git_helper_binary_is_judged_as_git(ctx: AppContext, repo: dict[str, Any]) -> None:
    assert GIT is not None
    exec_path = Path(git("--exec-path").strip())
    helper = exec_path / "git-send-pack"
    if not helper.is_file():
        pytest.skip("git-send-pack helper not installed")
    executables.save_entries(ctx.settings.data_dir, [str(GIT), str(helper)])
    pid = (
        AgentProfileService(ctx)
        .create(
            AgentProfileCreate(
                name="helper",
                adapter_type="generic-command",
                config={
                    "executable": str(helper),
                    "args": [str(repo["remote"]), "master"],
                    "prompt_mode": "none",
                },
            )
        )
        .id
    )
    assert_refused(ctx, run_in(ctx, pid, repo), repo)


def test_preflight_and_validation_commands_obey_the_policy(
    ctx: AppContext, repo: dict[str, Any]
) -> None:
    detail = run_in(
        ctx,
        git_profile(
            ctx,
            ["status"],
            preflight_commands=[[str(GIT), "log", "-1"]],
            validation_commands=[[str(GIT), "push", "origin", "master"]],
        ),
        repo,
    )
    # The validation push is refused before the preflight or the agent command run.
    assert_refused(ctx, detail, repo)
    assert "validation command refused" in (detail.run.failure_message or "")


def test_git_environment_injection_is_rejected(ctx: AppContext, repo: dict[str, Any]) -> None:
    for extra in (
        {"env_allowlist": ["GIT_CONFIG_PARAMETERS"]},
        {"env_allowlist": ["GIT_DIR"]},
        {"secret_env": {"GIT_SSH_COMMAND": "SOME_SECRET"}},
        {"env_allowlist": ["LD_PRELOAD"]},
    ):
        with pytest.raises(ValidationError):
            git_profile(ctx, ["status"], name=f"env-{len(str(extra))}", **extra)


def test_read_only_git_runs_and_commit_needs_approval(
    ctx: AppContext, repo: dict[str, Any]
) -> None:
    ok = run_in(ctx, git_profile(ctx, ["status", "--short"], name="status"), repo)
    assert ok.run.status == "succeeded", ok.run.failure_message
    (repo["work"] / "b.txt").write_text("b\n")
    git("add", "b.txt", cwd=repo["work"])
    pid = git_profile(
        ctx,
        ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-m", "x"],
        name="commit",
    )
    detail = run_in(ctx, pid, repo)
    assert detail.run.status == "waiting_for_approval"
    assert "command" not in event_types(ctx, detail.run.id)
    [approval] = ApprovalService(ctx).list_approvals(run_id=detail.run.id).items
    assert approval.type == "elevated_command"
    assert approval.requested_action["commands"][0]["argv"][0] == "git"
    ApprovalService(ctx).approve(approval.id, reviewer="tester")
    done = drive(ctx, detail.run.id)
    assert done.run.status == "succeeded", done.run.failure_message
    assert "x" in git("log", "-1", "--format=%s", cwd=repo["work"])
    assert not remote_has_commits(repo["remote"])


def test_rejected_command_approval_fails_the_run(ctx: AppContext, repo: dict[str, Any]) -> None:
    detail = run_in(ctx, git_profile(ctx, ["gc"], name="gc"), repo)
    assert detail.run.status == "waiting_for_approval"
    [approval] = ApprovalService(ctx).list_approvals(run_id=detail.run.id).items
    ApprovalService(ctx).reject(approval.id, reviewer="tester")
    assert drive(ctx, detail.run.id).run.status == "failed"
