"""The guarded Git runner refuses everything the V1 safety policy forbids."""

from __future__ import annotations

from pathlib import Path

import pytest

from agenttape.git.runner import GitPolicyError, GuardedGit, check_policy, is_owned_branch

ROOT = Path("/data/workspaces/git")


@pytest.mark.parametrize(
    "args",
    [
        ["push", "origin", "main"],
        ["push", "--force"],
        ["merge", "feature"],
        ["pull"],
        ["rebase", "main"],
        ["reset", "--hard", "HEAD~1"],
        ["reset", "HEAD"],
        ["clean", "-fdx"],
        ["checkout", "--", "."],
        ["switch", "main"],
        ["restore", "."],
        ["stash"],
        ["filter-branch"],
        ["update-ref", "-d", "refs/heads/main"],
        ["gc", "--prune=now"],
        ["fetch", "origin"],
        ["remote", "add", "x", "y"],
        ["tag", "-d", "v1"],
        ["cherry-pick", "abc"],
        ["revert", "abc"],
    ],
)
def test_forbidden_subcommands_are_rejected(args: list[str]) -> None:
    with pytest.raises(GitPolicyError):
        check_policy(args, managed_root=ROOT)


@pytest.mark.parametrize(
    "args",
    [
        ["branch", "-D", "agenttape/run-1"],
        ["branch", "-d", "main"],
        ["branch", "--delete", "feature/x"],
        ["branch", "-m", "agenttape/run-1", "x"],
        ["branch", "new-branch"],
        ["commit", "--amend", "-m", "x"],
        ["commit", "--no-verify", "-m", "x"],
        ["worktree", "remove", "--force", str(ROOT / "abc")],
        ["worktree", "remove", "/home/user/project"],
        ["worktree", "remove", str(ROOT)],
        ["worktree", "remove", str(ROOT / ".." / "elsewhere")],
        ["worktree", "add", "-b", "feature", str(ROOT / "abc"), "HEAD"],
        ["worktree", "add", "-b", "agenttape/run-1", "/tmp/outside", "HEAD"],
        ["worktree", "add", str(ROOT / "abc")],
        ["worktree", "add", "--detach", "/tmp/outside", "0123abcd"],
        ["worktree", "add", "--detach", str(ROOT / "abc"), "HEAD"],
        ["worktree", "add", "--detach", str(ROOT / "abc"), "main"],
        ["worktree", "add", "--detach", str(ROOT / "abc"), "--orphan"],
        ["worktree", "add", "--detach", str(ROOT / "abc"), "0123abcd", "extra"],
        ["worktree", "add", "--detach", str(ROOT / "abc")],
        ["worktree", "add", "--detach", "-b", "agenttape/run-1", str(ROOT / "abc"), "0123abcd"],
        ["worktree", "move", str(ROOT / "a"), str(ROOT / "b")],
        ["worktree", "lock", str(ROOT / "a")],
        ["config", "user.name", "x"],
        ["config", "--unset", "user.name"],
        ["-c", "core.pager=sh", "status"],
        ["diff", "--output=/etc/passwd"],
        ["symbolic-ref", "HEAD", "refs/heads/other"],
        ["add", "--force", "secret.env"],
        ["apply", "patch.diff"],
        [],
    ],
)
def test_destructive_forms_are_rejected(args: list[str]) -> None:
    with pytest.raises(GitPolicyError):
        check_policy(args, managed_root=ROOT)


@pytest.mark.parametrize(
    "args",
    [
        ["status", "--porcelain=v1"],
        ["diff", "--no-color", "HEAD"],
        ["rev-parse", "--verify", "HEAD"],
        ["symbolic-ref", "--quiet", "--short", "HEAD"],
        ["ls-files", "--others", "--exclude-standard", "-z"],
        ["rev-list", "--count", "a..b"],
        ["add", "--all", "--", "."],
        ["commit", "--quiet", "--file", "-"],
        ["branch", "-d", "agenttape/run-1234"],
        ["branch", "--list"],
        ["worktree", "list", "--porcelain"],
        ["worktree", "prune"],
        ["worktree", "add", "-b", "agenttape/run-1", str(ROOT / "run-1"), "abc123"],
        ["worktree", "add", "--detach", str(ROOT / "abc"), "0123abcd"],
        ["worktree", "remove", str(ROOT / "run-1")],
        ["config", "--get", "user.name"],
    ],
)
def test_allowed_forms_pass(args: list[str]) -> None:
    check_policy(args, managed_root=ROOT)


def test_worktree_changes_need_a_managed_root() -> None:
    with pytest.raises(GitPolicyError):
        check_policy(["worktree", "remove", str(ROOT / "a")], managed_root=None)


def test_owned_branch_prefix() -> None:
    assert is_owned_branch("agenttape/run-abc")
    assert not is_owned_branch("agenttape/run-")
    assert not is_owned_branch("main")
    assert not is_owned_branch("feature/agenttape/run-1")


def test_guarded_runner_refuses_before_starting_a_process(tmp_path: Path) -> None:
    git = GuardedGit(tmp_path)
    with pytest.raises(GitPolicyError) as err:
        git.run("push", "origin", "HEAD")
    assert "never pushes" in err.value.message
    assert err.value.details["git_args"][0] == "push"
