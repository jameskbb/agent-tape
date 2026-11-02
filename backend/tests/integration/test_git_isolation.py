"""Git isolation end to end in a temporary repository: worktree lifecycle, change capture,
protected paths, validations, owned-only cleanup, and the approved local commit."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from agenttape.adapters.base import (
    AdapterResult,
    AgentAdapter,
    DiscoveryResult,
    ExecutionContext,
    ProfileIssue,
)
from agenttape.adapters.process import CommandSpec, run_process
from agenttape.application.context import AppContext
from agenttape.application.schemas.catalog import (
    AgentProfileCreate,
    ProjectCreate,
    ProjectSettings,
)
from agenttape.application.services.approvals import ApprovalService
from agenttape.application.services.git import GitService
from agenttape.application.services.profiles import AgentProfileService
from agenttape.application.services.projects import ProjectService
from agenttape.domain.enums import AdapterAvailability, FailureCategory
from agenttape.domain.errors import ConflictError
from agenttape.git import hooks as git_hooks
from agenttape.infrastructure.db.extra_models.git_workspaces import RunWorkspace
from agenttape.infrastructure.db.models import AuditEntry, EvaluationResult, FileChange
from tests.conftest import drive, event_types, start_run

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test User",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test User",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}

EDIT_CODE = """
import os, pathlib
p = pathlib.Path('.')
(p / 'src' / 'app.py').write_text('print("changed")\\nprint("more")\\n')
(p / 'src' / 'new_module.py').write_text('VALUE = 1\\n')
os.remove('OBSOLETE.md')
os.rename('docs/guide.md', 'docs/manual.md')
(p / '.github' / 'workflows' / 'ci.yml').write_text('on: push\\njobs: {}\\n')
(p / 'assets' / 'logo.bin').write_bytes(bytes(range(256)) * 4)
print('edited in', os.getcwd())
"""


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, **GIT_ENV}
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, check=True
    )
    return result.stdout.strip()


def make_repo(root: Path) -> Path:
    repo = root / "app"
    (repo / "src").mkdir(parents=True)
    (repo / "docs").mkdir()
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / "assets").mkdir()
    (repo / "src" / "app.py").write_text('print("hello")\n')
    (repo / "OBSOLETE.md").write_text("old\n")
    (repo / "docs" / "guide.md").write_text("# Guide\n\n" + "Line of text.\n" * 20)
    (repo / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
    (repo / "assets" / "logo.bin").write_bytes(bytes(range(256)))
    git(repo, "init", "--quiet", "--initial-branch=main")
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", "Initial commit")
    return repo


class EditAdapter(AgentAdapter):
    """Runs ``python -c <config.code>`` in the prepared workdir."""

    name = "test_edit"
    display_name = "Test editor"

    def discover(self) -> DiscoveryResult:
        return DiscoveryResult(adapter=self.name, status=AdapterAvailability.READY, version="2.0")

    def validate_profile(self, config: dict[str, Any]) -> list[ProfileIssue]:
        return []

    async def execute(self, ctx: ExecutionContext) -> AdapterResult:
        result = await run_process(
            CommandSpec(argv=[sys.executable, "-c", ctx.profile["code"]], cwd=ctx.workdir),
            ctx,
        )
        if result.exit_code == 0:
            return AdapterResult.success(exit_code=0)
        return AdapterResult.failure(FailureCategory.ADAPTER_ERROR, "failed", exit_code=1)


@pytest.fixture
def repo(ctx: AppContext) -> Path:
    root = ctx.settings.allowed_roots[0]
    return make_repo(Path(root))


@pytest.fixture
def editor(ctx: AppContext) -> str:
    ctx.adapters.register(EditAdapter())
    return (
        AgentProfileService(ctx)
        .create(
            AgentProfileCreate(name="editor", adapter_type="test_edit", config={"code": EDIT_CODE})
        )
        .id
    )


def make_project(ctx: AppContext, repo: Path, *, isolate: bool = True, name: str = "App") -> str:
    return (
        ProjectService(ctx)
        .create(
            ProjectCreate(
                name=name,
                repo_path=str(repo),
                settings=ProjectSettings(
                    git_isolation=isolate,
                    protected_paths=[".github/**", "*.lock"],
                    validation_commands=[
                        [sys.executable, "-c", "print('validation ok')"],
                        [sys.executable, "-c", "import sys; sys.exit(3)"],
                    ],
                ),
            )
        )
        .id
    )


def test_an_untracked_file_past_the_count_cap_says_why_it_has_no_diff(
    ctx: AppContext, repo: Path, editor: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An untracked file left out of the diff because the run created more untracked
    files than AgentTape diffs is reported as ``count_cap``, never as over the size cap."""
    real_capture = git_hooks.capture_changes

    def capture_nothing_untracked(repo: Path, base: str) -> Any:
        return real_capture(repo, base, max_untracked_diffed=0)

    monkeypatch.setattr(git_hooks, "capture_changes", capture_nothing_untracked)
    project_id = make_project(ctx, repo)
    detail = drive(ctx, start_run(ctx, editor, project_id=project_id))
    assert detail.run.status == "succeeded", detail.run.failure_message

    view = GitService(ctx).view(detail.run.id)
    new_mod = next(f for f in view.files if f.path == "src/new_module.py")
    assert not new_mod.diff_available
    assert new_mod.diff_omitted == "count_cap"
    app = next(f for f in view.files if f.path == "src/app.py")
    assert app.diff_available and app.diff_omitted is None


def test_isolated_run_lifecycle(ctx: AppContext, repo: Path, editor: str) -> None:
    (repo / "notes.txt").write_text("my uncommitted notes\n")  # dirty before the run
    head_before = git(repo, "rev-parse", "HEAD")
    project_id = make_project(ctx, repo)
    detail = drive(ctx, start_run(ctx, editor, project_id=project_id))
    assert detail.run.status == "succeeded", detail.run.failure_message
    run_id = detail.run.id

    # The agent ran in an AgentTape-owned worktree on its own branch.
    worktree = ctx.settings.workspaces_dir / "git" / run_id
    assert detail.attempts[0].workdir == str(worktree)
    assert git(worktree, "symbolic-ref", "--short", "HEAD") == f"agenttape/run-{run_id}"
    assert not (worktree / "notes.txt").exists()  # started from the commit, not the dirty tree
    # The user's checkout is untouched.
    assert (repo / "src" / "app.py").read_text() == 'print("hello")\n'
    assert (repo / "OBSOLETE.md").exists()
    assert git(repo, "rev-parse", "HEAD") == head_before
    assert git(repo, "symbolic-ref", "--short", "HEAD") == "main"

    with ctx.session() as session:
        ws = session.scalars(select(RunWorkspace).where(RunWorkspace.run_id == run_id)).one()
        assert ws.isolated and ws.owned and ws.status == "active"
        assert ws.base_commit == head_before and ws.adapter_version == "2.0"
        changes = {
            c.path: c
            for c in session.scalars(select(FileChange).where(FileChange.run_id == run_id))
        }
        evals = session.scalars(
            select(EvaluationResult).where(EvaluationResult.run_id == run_id)
        ).all()

    assert changes["src/app.py"].change_type == "modified"
    assert changes["src/new_module.py"].change_type == "added"
    assert changes["OBSOLETE.md"].change_type == "deleted"
    assert changes["docs/manual.md"].change_type == "renamed"
    assert changes["docs/manual.md"].old_path == "docs/guide.md"
    assert changes[".github/workflows/ci.yml"].is_protected
    assert not changes["src/app.py"].is_protected
    logo = changes["assets/logo.bin"]
    assert logo.is_binary and logo.insertions is None and logo.size_bytes == 1024
    assert (changes["src/app.py"].insertions, changes["src/app.py"].deletions) == (2, 1)

    by_name = {e.name: e for e in evals}
    ok = next(e for n, e in by_name.items() if "validation ok" in n)
    bad = next(e for n, e in by_name.items() if "sys.exit(3)" in n)
    assert ok.status == "pass" and ok.evaluator_type == "validation_command"
    assert bad.status == "fail" and bad.evidence["exit_code"] == 3 and not bad.required

    types = event_types(ctx, run_id)
    assert "git_change" in types and "warning" in types and "evaluation" in types
    summaries = [e.summary for e in _events(ctx, run_id)]
    assert any("uncommitted file" in s for s in summaries)  # dirty-tree warning
    assert any(s.startswith("Protected path changed: .github/workflows/ci.yml") for s in summaries)
    assert any(s.startswith("Isolated worktree on agenttape/run-") for s in summaries)

    view = GitService(ctx).view(run_id)
    assert view.starting is not None and view.starting.head == head_before
    assert view.starting.dirty and "notes.txt" in view.starting.dirty_files
    assert view.summary.files == 6 and view.summary.protected_files == 1
    assert view.summary.renamed == 1 and view.summary.binary_files == 1
    app = next(f for f in view.files if f.path == "src/app.py")
    assert app.diff_available and app.hunks[0].lines
    new_mod = next(f for f in view.files if f.path == "src/new_module.py")
    assert new_mod.diff_available  # untracked files are in the diff too
    assert not next(f for f in view.files if f.path == "assets/logo.bin").hunks
    # Porcelain status lists the rename as a deletion plus an untracked file.
    assert view.workspace is not None and view.workspace.uncommitted_count == 7
    assert view.commit.available and view.commit.pending is None


def _events(ctx: AppContext, run_id: str) -> list[Any]:
    from agenttape.application.services.runs import RunService

    return RunService(ctx).list_events(run_id, limit=1000).items


def test_cleanup_refuses_uncommitted_work_then_commit_after_approval(
    ctx: AppContext, repo: Path, editor: str
) -> None:
    project_id = make_project(ctx, repo)
    run_id = drive(ctx, start_run(ctx, editor, project_id=project_id)).run.id
    worktree = ctx.settings.workspaces_dir / "git" / run_id
    branch = f"agenttape/run-{run_id}"
    service = GitService(ctx)

    refused = service.cleanup(run_id, actor="tester")
    assert not refused.removed and "uncommitted" in refused.message
    assert "src/app.py" in refused.uncommitted
    assert worktree.is_dir()

    approval = service.request_commit(run_id, message="Apply the agent's edit\n\nBody.", actor="me")
    assert approval.type == "commit" and approval.status == "pending"
    assert approval.risk_level == "high"  # a protected path is part of the change
    # Nothing is committed while the approval is pending.
    assert git(repo, "rev-list", "--count", branch) == "1"
    with pytest.raises(ConflictError):
        service.request_commit(run_id, message="again", actor="me")
    with pytest.raises(ConflictError):
        service.cleanup(run_id, actor="me")

    ApprovalService(ctx).approve(approval.id, reviewer="reviewer", note="looks right")
    assert git(repo, "rev-list", "--count", branch) == "2"
    assert git(repo, "log", "-1", "--format=%s", branch) == "Apply the agent's edit"
    assert git(repo, "rev-parse", "main") == git(repo, "rev-parse", f"{branch}~1")  # main unmoved
    assert git(worktree, "status", "--porcelain") == ""
    view = service.view(run_id)
    assert view.workspace is not None and view.workspace.last_commit_sha
    assert view.workspace.commits[0]["approval_id"] == approval.id
    assert not view.commit.available  # nothing left to commit

    with ctx.session() as session:
        actions = set(session.scalars(select(AuditEntry.action).where(AuditEntry.run_id == run_id)))
    assert {"git.commit.request", "approval.approved", "git.commit.create"} <= actions

    removed = service.cleanup(run_id, actor="tester")
    assert removed.removed and not worktree.exists()
    assert not removed.branch_deleted and "commit" in (removed.branch_kept_reason or "")
    assert git(repo, "rev-parse", "--verify", branch)  # the approved commit is kept
    assert service.view(run_id).workspace.status == "removed"  # type: ignore[union-attr]


def test_rejected_or_stale_commit_is_not_created(ctx: AppContext, repo: Path, editor: str) -> None:
    project_id = make_project(ctx, repo)
    run_id = drive(ctx, start_run(ctx, editor, project_id=project_id)).run.id
    branch = f"agenttape/run-{run_id}"
    service = GitService(ctx)
    first = service.request_commit(run_id, message="first", actor="me")
    ApprovalService(ctx).reject(first.id, reviewer="reviewer", note="no")
    assert git(repo, "rev-list", "--count", branch) == "1"

    second = service.request_commit(run_id, message="second", actor="me")
    worktree = ctx.settings.workspaces_dir / "git" / run_id
    (worktree / "src" / "app.py").write_text("edited after the request\n")
    ApprovalService(ctx).approve(second.id, reviewer="reviewer")
    assert git(repo, "rev-list", "--count", branch) == "1"
    summaries = [e.summary for e in _events(ctx, run_id)]
    assert any("worktree changed after the commit was requested" in s for s in summaries)


def test_clean_worktree_cleanup_deletes_the_empty_owned_branch(ctx: AppContext, repo: Path) -> None:
    ctx.adapters.register(EditAdapter())
    noop = (
        AgentProfileService(ctx)
        .create(
            AgentProfileCreate(name="noop", adapter_type="test_edit", config={"code": "print(1)"})
        )
        .id
    )
    project_id = make_project(ctx, repo)
    detail = drive(ctx, start_run(ctx, noop, project_id=project_id))
    assert detail.file_changes == []
    out = GitService(ctx).cleanup(detail.run.id, actor="tester")
    assert out.removed and out.branch_deleted
    branches = git(repo, "branch", "--list")
    assert f"agenttape/run-{detail.run.id}" not in branches and "main" in branches
    with pytest.raises(ConflictError):
        GitService(ctx).cleanup(detail.run.id, actor="tester")


def test_non_isolated_run_captures_changes_and_flags_pre_existing_dirt(
    ctx: AppContext, repo: Path
) -> None:
    ctx.adapters.register(EditAdapter())
    code = "import pathlib; pathlib.Path('src/app.py').write_text('x = 2\\n')"
    pid = (
        AgentProfileService(ctx)
        .create(AgentProfileCreate(name="inplace", adapter_type="test_edit", config={"code": code}))
        .id
    )
    (repo / "OBSOLETE.md").write_text("user edit\n")
    project_id = make_project(ctx, repo, isolate=False, name="Shared")
    detail = drive(ctx, start_run(ctx, pid, project_id=project_id))
    view = GitService(ctx).view(detail.run.id)
    files = {f.path: f for f in view.files}
    assert (
        files["src/app.py"].change_type == "modified" and not files["src/app.py"].pre_existing_dirty
    )
    assert files["OBSOLETE.md"].pre_existing_dirty
    assert view.workspace is not None and not view.workspace.isolated
    assert not view.commit.available and "isolated worktree" in (view.commit.reason or "")
    with pytest.raises(ConflictError):
        GitService(ctx).request_commit(detail.run.id, message="x", actor="me")


def test_isolation_on_a_non_repository_fails_without_touching_it(
    ctx: AppContext, editor: str
) -> None:
    plain = Path(ctx.settings.allowed_roots[0]) / "plain"
    plain.mkdir()
    (plain / "file.txt").write_text("keep me\n")
    project_id = make_project(ctx, plain, name="Plain")
    detail = drive(ctx, start_run(ctx, editor, project_id=project_id))
    assert detail.run.status == "failed"
    assert "not a Git repository" in (detail.run.failure_message or "") or any(
        "not a Git repository" in e.summary for e in _events(ctx, detail.run.id)
    )
    assert sorted(p.name for p in plain.iterdir()) == ["file.txt"]


def test_demo_agent_uses_the_isolated_fixture_worktree(ctx: AppContext) -> None:
    from agenttape.application.services.demo import DemoService
    from agenttape.seeds.git_compare_demo import FORK_TITLE, PARENT_TITLE

    report = DemoService(ctx).seed()
    parent_id, fork_id = report.data["compare_runs"]
    from agenttape.application.services.runs import RunService

    runs = RunService(ctx)
    parent, fork = runs.get_detail(parent_id), runs.get_detail(fork_id)
    assert parent.run.title == PARENT_TITLE and fork.run.title == FORK_TITLE
    assert parent.run.status == "succeeded" and fork.run.status == "succeeded"
    assert fork.run.parent_run_id == parent_id and fork.run.parent_relation == "fork"
    view = GitService(ctx).view(parent_id)
    paths = {f.path for f in view.files}
    assert {"src/greeter.py", "tests/test_greeter.py", "CHANGELOG.md"} <= paths
    assert next(f for f in view.files if f.path == "CHANGELOG.md").is_protected
    assert view.workspace is not None and view.workspace.isolated
    assert {v.status for v in view.validations} == {"pass"}
    # The fixture's own checkout is untouched.
    fixture = ctx.settings.data_dir / "fixtures" / "demo-repo"
    assert git(fixture, "status", "--porcelain") == ""
    # Seeding again is idempotent (no new runs).
    again = DemoService(ctx).seed()
    assert again.data["compare_runs"] == [parent_id]
