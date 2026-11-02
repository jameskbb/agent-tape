"""AgentTape's own Git calls never run programs named by repository files an agent can write.

Each test plants a vector (core.fsmonitor, diff.external, a clean filter selected by
.gitattributes, a textconv/diff driver, pagers, a swapped ``.git`` link file pointing at a
planted gitdir) and then calls the real AgentTape functions, asserting that no marker file
appears. ``test_vectors_are_live_with_plain_git`` proves the same plants do fire with an
unhardened Git, so the other tests are meaningful.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest

from agenttape.application.services.git import workspace_fingerprint
from agenttape.context_evidence.pack import _git_index
from agenttape.file_evidence.ledger import NoisePolicy
from agenttape.file_evidence.snapshot import end_snapshot, start_snapshot
from agenttape.git.changes import capture_changes, is_clean
from agenttape.git.invoke import (
    git_link_changed,
    harden_args,
    program_overrides,
    run_git_hardened,
    sidecar_path,
    trusted_git_dir,
)
from agenttape.git.manager import WorktreeManager
from agenttape.git.runner import GuardedGit
from agenttape.git.snapshot import capture_git_state, unified_diff, working_tree_changes

PLAIN_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}
ATTRIBUTES = "f.txt filter=x diff=d\ng.txt filter=a=b\n"


def plain_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=PLAIN_ENV, check=check
    )


def plant(git_dir: Path, markers: Path) -> None:
    """Write every program-running setting into the config of ``git_dir``."""
    markers.mkdir(parents=True, exist_ok=True)
    config = git_dir / "config"

    def touch(name: str, tail: str) -> str:
        return f"touch {shlex.quote(str(markers / name))}; {tail}"

    for key, value in {
        "core.fsmonitor": touch("fsmonitor", "true"),
        "diff.external": touch("diff-external", "true"),
        "filter.x.clean": touch("clean-filter", "cat"),
        "filter.x.smudge": touch("smudge-filter", "cat"),
        "filter.a=b.clean": touch("clean-filter-eq", "cat"),
        "diff.d.textconv": touch("textconv", "cat"),
        "diff.d.command": touch("diff-driver", "true"),
        "core.pager": touch("core-pager", "cat"),
        "pager.status": touch("pager-status", "cat"),
        "pager.diff": touch("pager-diff", "cat"),
        "core.hooksPath": str(markers / "hooks"),
    }.items():
        subprocess.run(
            ["git", "config", "--file", str(config), key, value], check=True, env=PLAIN_ENV
        )
    hooks = markers / "hooks"
    hooks.mkdir(exist_ok=True)
    for hook in ("pre-commit", "post-checkout", "post-index-change", "commit-msg"):
        script = hooks / hook
        script.write_text(f"#!/bin/sh\n{touch('hook-' + hook, 'true')}\n")
        script.chmod(0o755)


def fired(markers: Path) -> list[str]:
    return sorted(p.name for p in markers.iterdir() if p.is_file()) if markers.is_dir() else []


def make_repo(root: Path) -> Path:
    repo = root / "main"
    repo.mkdir(parents=True)
    plain_git(repo, "init", "--quiet", "--initial-branch=main")
    (repo / ".gitattributes").write_text(ATTRIBUTES)
    (repo / "f.txt").write_text("one\n")
    (repo / "g.txt").write_text("gee\n")
    plain_git(repo, "add", "-A")
    plain_git(repo, "commit", "--quiet", "-m", "init")
    return repo


def dirty(worktree: Path) -> None:
    """Changes that make status, diff, and add read the files (same size and new size)."""
    (worktree / "f.txt").write_text("two\n")  # same size: status must re-hash via clean
    os.utime(worktree / "f.txt", (1_000_000_000, 1_000_000_000))
    (worktree / "g.txt").write_text("changed g\n")
    (worktree / "new.txt").write_text("untracked\n")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path)


@pytest.fixture
def markers(tmp_path: Path) -> Path:
    return tmp_path / "markers"


def exercise_everything(path: Path, base: str) -> None:
    """Every AgentTape code path that runs Git against an agent-writable directory."""
    state = capture_git_state(path)
    assert state.is_repo and state.dirty, state
    assert working_tree_changes(path, base)
    assert "f.txt" in unified_diff(path, base)
    captured = capture_changes(path, base)
    assert {e.path for e in captured.entries} >= {"f.txt", "g.txt", "new.txt"}
    clean, _lines = is_clean(path)
    assert not clean
    assert workspace_fingerprint(path)
    git = GuardedGit(path)
    assert git.run("status", "--porcelain=v1").returncode == 0
    assert git.run("diff", base).returncode == 0
    assert git.run("log", "-p", "-1").returncode == 0
    assert git.run("show", "HEAD").returncode == 0
    start = start_snapshot(path, NoisePolicy())
    assert start["is_repo"]
    end = end_snapshot(path, NoisePolicy(), base=base, evidence_paths=["f.txt"])
    assert end["is_repo"]
    index = _git_index(path)
    assert index is not None and index["files"] >= 3


# ------------------------------------------------------------------------ sanity


def test_vectors_are_live_with_plain_git(repo: Path, markers: Path) -> None:
    """The plants really do run programs when Git is invoked without hardening."""
    plant(repo / ".git", markers)
    dirty(repo)
    plain_git(repo, "status", check=False)
    plain_git(repo, "diff", check=False)
    plain_git(repo, "diff", "--no-ext-diff", "--no-textconv", check=False)
    plain_git(repo, "log", "-p", "-1", "--no-ext-diff", check=False)
    got = set(fired(markers))
    assert {"fsmonitor", "diff-external", "clean-filter", "textconv"} <= got, got


# ------------------------------------------------------------------ one vector each

VECTORS = {
    # key: (plain git argv that fires it, AgentTape call that must not)
    "core.fsmonitor": (["status"], lambda r, b: capture_git_state(r)),
    "diff.external": (["diff"], lambda r, b: unified_diff(r, b)),
    "filter.x.clean": (["diff", "--no-ext-diff", "--no-textconv"], lambda r, b: is_clean(r)),
    "diff.d.textconv": (["diff", "--no-ext-diff"], lambda r, b: capture_changes(r, b)),
    "diff.d.command": (["diff"], lambda r, b: GuardedGit(r).run("diff", b)),
}


@pytest.mark.parametrize("key", sorted(VECTORS))
def test_single_vector_fires_plainly_but_not_through_agenttape(
    repo: Path, markers: Path, key: str
) -> None:
    markers.mkdir()
    marker = markers / "hit"
    tail = "cat" if key.startswith("filter") or key.endswith("textconv") else "true"
    plain_git(repo, "config", key, f"touch {shlex.quote(str(marker))}; {tail}")
    base = plain_git(repo, "rev-parse", "HEAD").stdout.strip()
    dirty(repo)
    trigger, call = VECTORS[key]
    call(repo, base)
    assert not marker.exists(), f"{key} ran through AgentTape"
    plain_git(repo, *trigger, check=False)
    assert marker.exists(), f"{key} is not a live vector with plain git"


# ------------------------------------------------------------------ non-isolated


def test_repo_config_programs_never_run_from_agenttape_calls(repo: Path, markers: Path) -> None:
    base = plain_git(repo, "rev-parse", "HEAD").stdout.strip()
    plant(repo / ".git", markers)
    dirty(repo)
    exercise_everything(repo, base)
    assert fired(markers) == []


def test_guarded_add_and_commit_run_no_filters_or_hooks(repo: Path, markers: Path) -> None:
    plant(repo / ".git", markers)
    dirty(repo)
    git = GuardedGit(repo)
    assert git.run("add", "--all", "--", ".").returncode == 0
    env = {"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "T"}
    env["GIT_COMMITTER_EMAIL"] = "t@x"
    result = git.run("commit", "--quiet", "--file", "-", env=env, input_text="msg\n")
    assert result.returncode == 0, result.stderr
    assert fired(markers) == []


def test_included_config_and_worktree_config_are_neutralized(repo: Path, markers: Path) -> None:
    included = repo / "planted.cfg"  # an agent-written file pulled in by include.path
    plant(repo / ".git", markers)
    (repo / ".git" / "config").write_text(
        f"[core]\n\trepositoryformatversion = 0\n\tbare = false\n[include]\n\tpath = {included}\n"
    )
    included.write_text(
        f'[filter "x"]\n\tclean = touch {markers / "included-clean"}; cat\n'
        f"[core]\n\tfsmonitor = touch {markers / 'included-fsmonitor'}; true\n"
    )
    base = plain_git(repo, "rev-parse", "HEAD").stdout.strip()
    dirty(repo)
    exercise_everything(repo, base)
    assert fired(markers) == []


def test_unreadable_config_fails_closed(repo: Path, markers: Path) -> None:
    (repo / ".git" / "config").write_text("[core\n\tfsmonitor = oops\n")
    result = run_git_hardened(repo, "status")
    assert result.returncode != 0
    assert "could not read the Git configuration safely" in result.stderr
    state = capture_git_state(repo)
    assert not state.dirty


# -------------------------------------------------------- swapped .git link file


def planted_gitdir(tmp_path: Path, markers: Path) -> Path:
    """A real repository (same tree) whose config runs programs."""
    evil = tmp_path / "evil"
    evil.mkdir()
    plain_git(evil, "init", "--quiet", "--initial-branch=main")
    (evil / ".gitattributes").write_text(ATTRIBUTES)
    (evil / "f.txt").write_text("one\n")
    (evil / "g.txt").write_text("gee\n")
    plain_git(evil, "add", "-A")
    plain_git(evil, "commit", "--quiet", "-m", "planted")
    plant(evil / ".git", markers)
    return evil / ".git"


def test_managed_worktree_ignores_a_swapped_git_link(
    tmp_path: Path, repo: Path, markers: Path
) -> None:
    manager = WorktreeManager(tmp_path / "workspaces")
    prepared = manager.prepare(run_id="run1", project_path=repo, isolate=True)
    wt = prepared.path
    base = prepared.base_commit
    assert base is not None
    recorded = trusted_git_dir(wt)
    assert recorded is not None and recorded.is_relative_to((repo / ".git").resolve())
    assert sidecar_path(wt).is_file() and not git_link_changed(wt)

    evil = planted_gitdir(tmp_path, markers)
    (wt / ".git").write_text(f"gitdir: {evil}\n")  # the agent rewrites its .git file
    assert git_link_changed(wt)
    dirty(wt)
    exercise_everything(wt, base)
    assert fired(markers) == []
    # Git kept using the recorded worktree, not the planted repository.
    state = capture_git_state(wt)
    assert state.head == base and state.branch == "agenttape/run-run1"
    assert WorktreeManager.main_repo_of(wt) == repo.resolve()


def test_managed_worktree_cleanup_forgets_the_record(tmp_path: Path, repo: Path) -> None:
    manager = WorktreeManager(tmp_path / "workspaces")
    prepared = manager.prepare(run_id="run2", project_path=repo, isolate=True)
    result = manager.cleanup_worktree(
        repo_path=repo, path=prepared.path, branch=prepared.branch, base_commit=prepared.base_commit
    )
    assert result.removed, result.message
    assert not sidecar_path(prepared.path).exists()


def test_unmanaged_repo_with_swapped_git_link_runs_nothing(
    tmp_path: Path, repo: Path, markers: Path
) -> None:
    """Without a record, a planted gitdir is still local-scope config and is neutralized."""
    wt = tmp_path / "loose"
    plain_git(repo, "worktree", "add", "--quiet", "-b", "loose", str(wt))
    evil = planted_gitdir(tmp_path, markers)
    base = plain_git(evil.parent, "rev-parse", "HEAD").stdout.strip()
    (wt / ".git").write_text(f"gitdir: {evil}\n")
    dirty(wt)
    exercise_everything(wt, base)
    assert fired(markers) == []


# ------------------------------------------------------------------------ units


def test_program_overrides_blank_repo_scoped_programs_only() -> None:
    listing = "\0".join(
        [
            "global",
            "filter.lfs.clean",
            "local",
            "filter.X.clean",
            "local",
            "filter.a=b.process",
            "worktree",
            "diff.d.textconv",
            "local",
            "core.fsmonitor",
            "local",
            "user.name",
            "local",
            "credential.https://x.helper",
            "command",
            "diff.external",
        ]
    )
    assert program_overrides(listing + "\0") == [
        "filter.X.clean",
        "filter.a=b.process",
        "diff.d.textconv",
        "credential.https://x.helper",
    ]


def test_harden_args_adds_no_program_flags() -> None:
    assert harden_args(["diff", "HEAD"]) == [
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--ignore-submodules=dirty",
        "HEAD",
    ]
    assert harden_args(["log", "-p"])[:3] == ["log", "--no-ext-diff", "--no-textconv"]
    assert harden_args(["status"]) == ["status", "--ignore-submodules=dirty"]
    assert harden_args(["rev-parse", "HEAD"]) == ["rev-parse", "HEAD"]
