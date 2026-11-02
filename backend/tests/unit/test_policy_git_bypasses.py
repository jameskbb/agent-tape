"""Git policy bypass attempts (pure policy, no I/O)."""

from __future__ import annotations

import pytest

from agenttape.domain.policy import Verdict, check_command, check_git, parse_git


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "-c", "alias.p=push", "p"],
        ["git", "-cALIAS.P=push", "p"],
        ["git", "--config-env", "alias.p=SOMEVAR", "p"],
        ["git", "--config-env=alias.p=SOMEVAR", "p"],
        ["git", "--exec-path=/tmp/evil", "status"],
        ["git", "-C", "/repo", "push"],
        ["git", "--git-dir", "/repo/.git", "--work-tree", "/repo", "push"],
        ["git", "push", "-fu", "origin", "main"],
        ["git", "push", "-du", "origin", "main"],
        ["git", "send-pack", "origin", "main"],
        ["git", "http-push", "origin"],
        ["git", "branch", "-Df", "x"],
        ["git", "branch", "--delete", "x"],
        ["git", "tag", "-d", "v1"],
        ["git", "symbolic-ref", "-d", "HEAD"],
        ["git", "clean", "--force"],
        ["git", "switch", "--discard-changes", "main"],
        ["git", "checkout", "-fb", "x"],
        ["/usr/lib/git-core/git-push", "origin"],
        ["git-merge", "other"],
        ["env", "GIT_DIR=/x", "git", "push"],
        ["timeout", "5", "git", "push"],
    ],
)
def test_bypasses_are_denied(argv: list[str]) -> None:
    assert check_command(argv, allowlist=[argv[0]]).verdict == Verdict.DENY


@pytest.mark.parametrize(
    "argv",
    [
        # ``-c`` keys that can run programs are now refused outright (see
        # test_policy_injection_hardening.py); harmless keys still need an approval.
        ["git", "-c", "user.name=t", "status"],
        ["git", "fetch", "origin"],
        ["git", "config", "alias.x", "push"],
        ["git", "config", "--add", "core.pager", "evil"],
        ["git", "remote", "set-url", "origin", "x"],
        ["git", "filter-branch"],
    ],
)
def test_risky_git_needs_approval(argv: list[str]) -> None:
    assert check_git(argv).verdict == Verdict.REQUIRE_APPROVAL


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "status"],
        ["git", "-C", "/repo", "log", "-1"],
        ["git", "config", "user.name"],
        ["git", "config", "--get", "user.email"],
        ["git", "branch", "--list"],
        ["git", "diff", "--stat"],
    ],
)
def test_read_only_git_is_allowed(argv: list[str]) -> None:
    assert check_git(argv).verdict == Verdict.ALLOW


def test_parse_git_collects_global_options_and_config() -> None:
    inv = parse_git(["git", "-C", "/r", "-c", "a.b=c", "--no-pager", "log", "-1"])
    assert inv.sub == "log" and inv.rest == ["-1"]
    assert inv.global_opts == ["-C", "/r", "-c", "a.b=c", "--no-pager"]
    assert inv.config == [("a.b", "c")]


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "worktree", "add", "x", "HEAD"],
        ["git", "worktree", "add", "--detach", "x", "HEAD"],
        ["git", "worktree", "add", "-b", "feature", "x", "HEAD"],
        ["git", "worktree", "move", "a", "b"],
        ["git", "worktree", "lock", "a"],
        ["git", "worktree", "unlock", "a"],
        ["git", "worktree", "repair"],
        ["git", "-C", "sub", "worktree", "add", "x"],
        ["git", "branch", "experimental"],
        ["git", "branch", "experimental", "HEAD~1"],
        ["git", "branch", "-q", "experimental"],
        ["git", "branch", "-c", "main", "copy"],
        ["git", "branch", "-C", "main", "copy"],
        ["git", "branch", "--copy", "main", "copy"],
        ["git", "branch", "-u", "origin/main"],
        ["git", "branch", "--set-upstream-to=origin/main"],
        ["git", "branch", "--unset-upstream"],
        ["git", "branch", "--track", "x", "origin/x"],
        ["git", "branch", "--edit-description"],
        ["git", "branch", "--", "experimental"],
    ],
)
def test_worktree_and_branch_changes_need_approval(argv: list[str]) -> None:
    """Adding, moving, or locking a worktree and creating, copying, or reconfiguring a
    branch change the repository, so they are never ALLOW."""
    decision = check_command(argv)
    assert decision.verdict == Verdict.REQUIRE_APPROVAL, decision
    assert check_git(argv).verdict == Verdict.REQUIRE_APPROVAL


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "branch", "-m", "old", "new"],
        ["git", "branch", "-M", "old", "new"],
        ["git", "branch", "--move", "old", "new"],
        ["git", "worktree", "prune"],
    ],
)
def test_branch_rename_and_worktree_prune_stay_denied(argv: list[str]) -> None:
    assert check_command(argv).verdict == Verdict.DENY


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "branch"],
        ["git", "branch", "--list"],
        ["git", "branch", "--list", "agenttape/*"],
        ["git", "branch", "-l", "feat*"],
        ["git", "branch", "-a"],
        ["git", "branch", "-r"],
        ["git", "branch", "-v"],
        ["git", "branch", "-vv"],
        ["git", "branch", "-av"],
        ["git", "branch", "--show-current"],
        ["git", "branch", "--contains", "HEAD"],
        ["git", "branch", "--no-merged", "main"],
        ["git", "branch", "--merged"],
        ["git", "branch", "--points-at", "HEAD"],
        ["git", "branch", "--sort=-committerdate"],
        ["git", "branch", "--sort", "refname"],
        ["git", "branch", "--format=%(refname)"],
        ["git", "branch", "--no-color", "--no-column"],
        ["git", "worktree", "list"],
        ["git", "worktree", "list", "--porcelain"],
        ["git", "worktree"],
    ],
)
def test_worktree_and_branch_listings_stay_allowed(argv: list[str]) -> None:
    assert check_command(argv).verdict == Verdict.ALLOW, check_command(argv)
