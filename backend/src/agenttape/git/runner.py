"""The guarded Git runner: the only way AgentTape itself runs Git commands that may write.

Every invocation is an argument array (never a shell) and is checked against a policy
before it runs. The V1 safety policy is enforced here, not by
convention:

* never ``push``, ``merge``, ``pull``, ``rebase``, ``reset``, ``clean``, ``checkout``, or
  any other history- or working-tree-destroying subcommand;
* never force flags (``--force``/``-f``/``-D``/``--hard``) and never ``commit --amend``;
* branches are only created through ``worktree add -b agenttape/run-...`` and only
  AgentTape-owned branches (that prefix) may be deleted, with the safe ``-d``;
* worktrees may only be added or removed inside the AgentTape-managed directory.

A rejected command raises :class:`GitPolicyError` (a ``PolicyViolationError``) before any
process starts. Allowed commands run through :func:`agenttape.git.invoke.run_git_hardened`,
so repository configuration an agent can write never makes Git run a program.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from pathlib import Path

from agenttape.domain.errors import PolicyViolationError
from agenttape.git.invoke import (
    DEFAULT_TIMEOUT_SECONDS,
    base_argv,
    git_executable,
    harden_args,
    hardened_env,
    run_git_hardened,
)
from agenttape.security.paths import is_within

OWNED_BRANCH_PREFIX = "agenttape/run-"

#: A Git commit hash, short or full: never a ref name like ``HEAD`` or ``main``, only a
#: resolved hex commit (a reproduction snapshot's pinned commit).
HEX_COMMIT = re.compile(r"[0-9a-f]{7,64}")

#: Subcommands that are never allowed, with the reason shown to the user.
FORBIDDEN: dict[str, str] = {
    "push": "AgentTape never pushes",
    "merge": "AgentTape never merges",
    "pull": "AgentTape never pulls (it would merge or rebase)",
    "rebase": "AgentTape never rewrites history",
    "reset": "AgentTape never resets branches or the working tree",
    "clean": "AgentTape never deletes untracked files",
    "checkout": "checkout can discard working-tree changes",
    "switch": "switch can discard working-tree changes",
    "restore": "restore discards working-tree changes",
    "stash": "stash hides uncommitted work",
    "cherry-pick": "AgentTape never rewrites history",
    "revert": "AgentTape never rewrites history",
    "am": "AgentTape never applies mailboxes",
    "filter-branch": "AgentTape never rewrites history",
    "filter-repo": "AgentTape never rewrites history",
    "update-ref": "AgentTape never moves refs directly",
    "gc": "AgentTape never prunes objects",
    "fetch": "AgentTape never talks to remotes",
    "remote": "AgentTape never changes remotes",
    "tag": "AgentTape never creates or deletes tags",
    "replace": "AgentTape never replaces objects",
    "notes": "AgentTape never writes notes",
}

#: Read-only subcommands that take arbitrary arguments.
READ_ONLY = frozenset(
    {
        "rev-parse",
        "symbolic-ref",
        "status",
        "diff",
        "ls-files",
        "log",
        "show",
        "cat-file",
        "rev-list",
        "for-each-ref",
        "merge-base",
    }
)

#: Flags rejected for every subcommand that could otherwise destroy work.
FORCE_FLAGS = frozenset({"--force", "-f", "-D", "--hard", "--force-with-lease", "-ff"})


class GitPolicyError(PolicyViolationError):
    """A Git command AgentTape refuses to run."""


def is_owned_branch(name: str) -> bool:
    return name.startswith(OWNED_BRANCH_PREFIX) and len(name) > len(OWNED_BRANCH_PREFIX)


def owned_branch_name(run_id: str) -> str:
    return f"{OWNED_BRANCH_PREFIX}{run_id}"


def _reject(message: str, argv: Sequence[str]) -> GitPolicyError:
    return GitPolicyError(message, details={"git_args": list(argv)[:12]})


def check_policy(args: Sequence[str], *, managed_root: Path | None) -> None:
    """Raise :class:`GitPolicyError` unless ``args`` (``[subcommand, ...]``) is allowed."""
    if not args:
        raise _reject("empty git command", args)
    sub, rest = args[0], list(args[1:])
    if sub.startswith("-"):
        raise _reject("global git options are set by AgentTape, not by callers", args)
    if sub in FORBIDDEN:
        raise _reject(f"git {sub} is not allowed: {FORBIDDEN[sub]}", args)
    if sub in READ_ONLY:
        if any(a.startswith("--output") for a in rest):
            raise _reject(f"git {sub} may not write output files", args)
        if sub == "symbolic-ref" and len([a for a in rest if not a.startswith("-")]) > 1:
            raise _reject("git symbolic-ref may only read a ref", args)
        return
    if any(a in FORCE_FLAGS for a in rest):
        raise _reject(f"force flags are not allowed for git {sub}", args)
    if sub == "config":
        if rest[:1] != ["--get"] or len(rest) != 2:
            raise _reject("only 'git config --get <key>' is allowed", args)
        return
    if sub == "add":
        return
    if sub == "commit":
        if "--amend" in rest:
            raise _reject("git commit --amend is not allowed", args)
        if "--no-verify" in rest or "-n" in rest:
            raise _reject("commit flags that skip checks are set by AgentTape only", args)
        return
    if sub == "branch":
        _check_branch(rest, args)
        return
    if sub == "worktree":
        _check_worktree(rest, args, managed_root)
        return
    raise _reject(f"git {sub} is not on AgentTape's allowlist", args)


def _check_branch(rest: list[str], argv: Sequence[str]) -> None:
    if not rest or rest in (["--show-current"], ["--list"]) or rest[0] == "--list":
        return
    if rest[0] in ("-d", "--delete") and len(rest) == 2:
        if not is_owned_branch(rest[1]):
            raise _reject(
                f"branch '{rest[1]}' was not created by AgentTape and cannot be deleted", argv
            )
        return
    raise _reject("only listing branches or deleting an AgentTape-owned branch is allowed", argv)


def _check_worktree(rest: list[str], argv: Sequence[str], managed_root: Path | None) -> None:
    if not rest:
        raise _reject("git worktree needs a subcommand", argv)
    action, params = rest[0], rest[1:]
    if action == "list" or action == "prune":
        return
    if managed_root is None:
        raise _reject("worktree changes need an AgentTape-managed root", argv)
    root = managed_root.resolve()
    if action == "add":
        # Expected shapes: 'add -b <agenttape/run-...> <path> <commit>' (a run's own
        # worktree, on an AgentTape-owned branch) or 'add --detach <path> <hex commit>' (a
        # reproduction's branchless snapshot - no branch is ever created, so none can ever
        # be deleted either).
        if len(params) == 3 and params[0] == "--detach":
            path, commit = params[1], params[2]
            if not is_within(Path(path).resolve(), root):
                raise _reject("worktrees may only be created in the AgentTape workspaces dir", argv)
            if not HEX_COMMIT.fullmatch(commit):
                raise _reject("worktree add --detach needs a hex commit", argv)
            return
        if len(params) != 4 or params[0] != "-b":
            raise _reject(
                "worktree add must be 'add -b <owned branch> <path> <commit>' or "
                "'add --detach <path> <commit>'",
                argv,
            )
        branch, path = params[1], params[2]
        if not is_owned_branch(branch):
            raise _reject(f"worktree branches must start with {OWNED_BRANCH_PREFIX}", argv)
        if not is_within(Path(path).resolve(), root):
            raise _reject("worktrees may only be created in the AgentTape workspaces dir", argv)
        return
    if action == "remove":
        if len(params) != 1:
            raise _reject("worktree remove takes exactly one path (never --force)", argv)
        target = Path(params[0]).resolve()
        if not is_within(target, root) or target == root:
            raise _reject("only AgentTape-owned worktrees can be removed", argv)
        return
    raise _reject(f"git worktree {action} is not allowed", argv)


def git_env() -> dict[str, str]:
    """Environment for AgentTape's own Git calls (see :func:`agenttape.git.invoke.hardened_env`)."""
    return hardened_env()


class GuardedGit:
    """Run policy-checked Git commands in ``repo``.

    ``managed_root`` is the AgentTape-owned directory worktrees live in; worktree add and
    remove are refused anywhere else.
    """

    def __init__(self, repo: Path, *, managed_root: Path | None = None) -> None:
        self.repo = Path(repo)
        self.managed_root = managed_root

    @staticmethod
    def executable() -> str:
        return git_executable()

    def argv(self, *args: str) -> list[str]:
        """The hardened argv (without the per-call config overrides, which go in the env)."""
        return [*base_argv(self.repo), *harden_args(args)]

    def run(
        self,
        *args: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        env: dict[str, str] | None = None,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        check_policy(args, managed_root=self.managed_root)
        # Argument array, policy-checked above; hardened against repository-configured
        # programs (fsmonitor, filters, external diff, hooks, a swapped .git link).
        return run_git_hardened(self.repo, *args, timeout=timeout, env=env, input_text=input_text)

    def out(self, *args: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> str:
        """Run and return stdout, or raise ``RuntimeError`` with stderr on failure."""
        result = self.run(*args, timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout).strip() or f"git {args[0]} failed")
        return result.stdout
