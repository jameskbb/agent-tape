"""The V1 safety policy: hard-disallowed actions and approval-required defaults."""

from __future__ import annotations

import pytest

from agenttape.domain.policy import (
    APPROVAL_REQUIRED_BY_DEFAULT,
    ApprovalType,
    DisallowedAction,
    Verdict,
    check_budget_change,
    check_command,
    check_commit,
    check_evaluation_override,
    check_protected_write,
    matches_protected,
)


@pytest.mark.parametrize(
    ("argv", "action"),
    [
        (["git", "push"], DisallowedAction.AUTOMATIC_PUSH),
        (["git", "-C", "/repo", "push", "origin", "main"], DisallowedAction.AUTOMATIC_PUSH),
        (["git", "push", "--force"], DisallowedAction.FORCE_PUSH),
        (["git", "push", "origin", "+main"], DisallowedAction.FORCE_PUSH),
        (
            ["git", "push", "--delete", "origin", "x"],
            DisallowedAction.DELETE_FOREIGN_BRANCH_OR_WORKTREE,
        ),
        (["git", "merge", "feature"], DisallowedAction.AUTOMATIC_MERGE),
        (["git", "pull"], DisallowedAction.AUTOMATIC_MERGE),
        (["git", "rebase", "main"], DisallowedAction.AUTOMATIC_MERGE),
        (["gh", "pr", "merge", "1"], DisallowedAction.AUTOMATIC_MERGE),
        (["git", "reset", "--hard", "HEAD~1"], DisallowedAction.DESTRUCTIVE_RESET),
        (["git", "clean", "-fdx"], DisallowedAction.DESTRUCTIVE_RESET),
        (["git", "checkout", "--", "."], DisallowedAction.DESTRUCTIVE_RESET),
        (["git", "stash", "drop"], DisallowedAction.DESTRUCTIVE_RESET),
        (["git", "branch", "-D", "main"], DisallowedAction.DELETE_FOREIGN_BRANCH_OR_WORKTREE),
        (
            ["git", "worktree", "remove", "/elsewhere"],
            DisallowedAction.DELETE_FOREIGN_BRANCH_OR_WORKTREE,
        ),
        (["rm", "-rf", "/"], DisallowedAction.HOST_FILE_DELETION),
        (["/bin/rm", "file"], DisallowedAction.HOST_FILE_DELETION),
        (["find", ".", "-delete"], DisallowedAction.HOST_FILE_DELETION),
        (["printenv"], DisallowedAction.DISPLAY_SECRETS),
        (["env"], DisallowedAction.DISPLAY_SECRETS),
        (["env", "X=1", "git", "push"], DisallowedAction.AUTOMATIC_PUSH),
        (["timeout", "5", "git", "push"], DisallowedAction.AUTOMATIC_PUSH),
    ],
)
def test_hard_disallowed_even_with_approval(argv: list[str], action: DisallowedAction) -> None:
    decision = check_command(argv, allowlist=["git", "rm", "find", "gh", "env", "printenv"])
    assert decision.verdict == Verdict.DENY, decision
    assert decision.disallowed == action


@pytest.mark.parametrize("argv", [["bash", "-c", "ls"], ["sh", "script.sh"], ["sudo", "ls"]])
def test_shells_and_privilege_escalation_are_refused(argv: list[str]) -> None:
    assert check_command(argv).verdict == Verdict.DENY


def test_owned_worktree_removal_is_allowed_only_for_agenttape_worktrees() -> None:
    owned = ["/data/workspaces/wt-1"]
    ok = check_command(
        ["git", "worktree", "remove", "/data/workspaces/wt-1"], owned_worktrees=owned
    )
    assert ok.verdict == Verdict.ALLOW
    bad = check_command(["git", "worktree", "remove", "/data/other"], owned_worktrees=owned)
    assert bad.verdict == Verdict.DENY


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "status"],
        ["git", "diff", "--stat"],
        ["pytest", "-q"],
        ["make", "test"],
        ["echo", "hi"],
    ],
)
def test_allowlisted_commands_are_allowed(argv: list[str]) -> None:
    assert check_command(argv).verdict == Verdict.ALLOW


def test_commands_outside_the_allowlist_or_elevated_need_approval() -> None:
    outside = check_command(["python", "-c", "print(1)"])
    assert outside.verdict == Verdict.REQUIRE_APPROVAL
    assert outside.approval_type == ApprovalType.ELEVATED_COMMAND
    elevated = check_command(["git", "status"], elevated=True)
    assert elevated.verdict == Verdict.REQUIRE_APPROVAL
    custom = check_command(["python", "-V"], allowlist=["python"])
    assert custom.verdict == Verdict.ALLOW


def test_trusted_validation_commands_match_exactly() -> None:
    trusted = [["python", "-m", "compileall", "-q", "src"]]
    assert (
        check_command(["python", "-m", "compileall", "-q", "src"], trusted_commands=trusted).verdict
        == Verdict.ALLOW
    )
    assert (
        check_command(["python", "-c", "import os"], trusted_commands=trusted).verdict
        == Verdict.REQUIRE_APPROVAL
    )
    # Trust never overrides the hard-disallow list.
    assert (
        check_command(["git", "push"], trusted_commands=[["git", "push"]]).verdict == Verdict.DENY
    )


def test_git_commit_requires_approval() -> None:
    assert check_command(["git", "commit", "-m", "x"]).approval_type == ApprovalType.GIT_COMMIT
    assert check_commit().verdict == Verdict.REQUIRE_APPROVAL


def test_protected_paths() -> None:
    patterns = [".github/**", "*.lock", "secrets/"]
    assert matches_protected(".github/workflows/ci.yml", patterns) == [".github/**"]
    assert matches_protected("backend/uv.lock", patterns) == ["*.lock"]
    assert matches_protected("secrets/key.txt", patterns) == ["secrets/"]
    assert matches_protected("src/app.py", patterns) == []
    decision = check_protected_write(["src/a.py", ".github/x.yml"], patterns)
    assert decision.approval_type == ApprovalType.PROTECTED_PATH
    assert decision.details["paths"] == [".github/x.yml"]
    assert check_protected_write(["src/a.py"], patterns).verdict == Verdict.ALLOW


def test_budget_increase_needs_approval_but_decrease_does_not() -> None:
    assert check_budget_change({"max_cost": 5}, {"max_cost": 3}).verdict == Verdict.ALLOW
    up = check_budget_change({"max_cost": 5, "max_tokens": 10}, {"max_cost": 9, "max_tokens": 10})
    assert up.approval_type == ApprovalType.BUDGET_INCREASE
    assert up.details["increases"] == {"max_cost": {"from": 5, "to": 9}}
    removed = check_budget_change({"max_cost": 5}, {})
    assert removed.verdict == Verdict.REQUIRE_APPROVAL
    assert check_budget_change(None, {"max_cost": 100}).verdict == Verdict.ALLOW


def test_evaluation_override() -> None:
    assert check_evaluation_override(override_permitted=True, failed=[]).verdict == Verdict.ALLOW
    assert (
        check_evaluation_override(override_permitted=False, failed=["tests"]).verdict
        == Verdict.DENY
    )
    ok = check_evaluation_override(override_permitted=True, failed=["tests"])
    assert ok.approval_type == ApprovalType.EVALUATION_OVERRIDE


def test_default_approval_types_are_complete() -> None:
    assert {t.value for t in APPROVAL_REQUIRED_BY_DEFAULT} == {
        "commit",
        "elevated_command",
        "protected_path",
        "budget_increase",
        "evaluation_override",
    }
