"""AgentTape-owned Git worktrees: create per run, clean up only when nothing is lost.

``prepare`` returns the directory a run executes in (the project repo itself, or an
AgentTape-owned worktree); ``cleanup`` removes only worktrees AgentTape created. Worktrees
live under ``<data_dir>/workspaces/git/<run_id>`` on branch ``agenttape/run-<run_id>``,
created from the base commit recorded in the run snapshot. Every Git call goes through
:class:`agenttape.git.runner.GuardedGit`, so the policy (no push, merge, reset, force,
or deletion of branches AgentTape did not create) holds even if this code is wrong.
Right after creating a worktree its admin git dir is recorded in a sidecar next to it, so
later Git calls pass ``--git-dir``/``--work-tree`` instead of trusting the worktree's
agent-writable ``.git`` file.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agenttape.git.changes import head_commit, is_clean
from agenttape.git.invoke import forget_worktree_git_dir, record_worktree_git_dir
from agenttape.git.runner import HEX_COMMIT, GuardedGit, is_owned_branch, owned_branch_name
from agenttape.security.paths import is_within

MANAGED_SUBDIR = "git"


@dataclass
class PreparedWorkspace:
    path: Path
    isolated: bool
    base_commit: str | None
    branch: str | None = None
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class IsolationError(RuntimeError):
    """Isolation was requested but a worktree could not be created."""


@dataclass
class CleanupResult:
    removed: bool
    message: str
    path: str
    branch: str | None = None
    branch_deleted: bool = False
    branch_kept_reason: str | None = None
    uncommitted: list[str] = field(default_factory=list)


def managed_root(workspaces_dir: Path) -> Path:
    return workspaces_dir / MANAGED_SUBDIR


class WorktreeManager:
    def __init__(self, workspaces_dir: Path) -> None:
        self.root = managed_root(workspaces_dir)

    def worktree_path(self, run_id: str) -> Path:
        if not run_id or "/" in run_id or run_id in (".", ".."):
            raise ValueError("invalid run id")
        return self.root / run_id

    def owns_path(self, path: Path) -> bool:
        try:
            resolved = path.resolve()
        except OSError:
            return False
        return is_within(resolved, self.root.resolve()) and resolved != self.root.resolve()

    # ------------------------------------------------------------------ prepare

    def prepare(
        self,
        *,
        run_id: str,
        project_path: Path,
        isolate: bool,
        base_commit: str | None = None,
    ) -> PreparedWorkspace:
        if not isolate:
            return PreparedWorkspace(
                path=project_path, isolated=False, base_commit=head_commit(project_path)
            )
        path = self.worktree_path(run_id)
        branch = owned_branch_name(run_id)
        if path.exists():
            # Resuming or retrying the same run: reuse the worktree it already has.
            current = GuardedGit(path).run("symbolic-ref", "--quiet", "--short", "HEAD")
            if current.returncode == 0 and current.stdout.strip() == branch:
                return PreparedWorkspace(
                    path=path,
                    isolated=True,
                    base_commit=base_commit,
                    branch=branch,
                    metadata={"reused": True},
                )
            raise IsolationError(
                f"{path} exists but is not the worktree AgentTape created for this run"
            )
        base = base_commit or head_commit(project_path)
        if base is None:
            raise IsolationError(
                "Git isolation needs a repository with at least one commit; "
                f"{project_path} has none"
            )
        self.root.mkdir(parents=True, exist_ok=True)
        git = GuardedGit(project_path, managed_root=self.root)
        exists = git.run("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
        if exists.returncode == 0:
            raise IsolationError(f"branch {branch} already exists; refusing to reuse it")
        result = git.run("worktree", "add", "-b", branch, str(path), base, timeout=120)
        if result.returncode != 0:
            raise IsolationError(
                "git worktree add failed: " + (result.stderr or result.stdout).strip()[:500]
            )
        try:
            # Record the admin git dir now, before any agent runs, so later Git calls
            # never trust the agent-writable ``.git`` link file in the worktree.
            record_worktree_git_dir(path)
        except (OSError, RuntimeError) as exc:
            raise IsolationError(f"could not record the worktree's git dir: {exc}") from exc
        return PreparedWorkspace(path=path, isolated=True, base_commit=base, branch=branch)

    # ------------------------------------------------------------------ cleanup

    def cleanup(self, workspace: PreparedWorkspace) -> bool:
        if not workspace.isolated:
            return False
        repo = workspace.metadata.get("repo_path")
        result = self.cleanup_worktree(
            repo_path=Path(repo) if repo else None,
            path=workspace.path,
            branch=workspace.branch,
            base_commit=workspace.base_commit,
        )
        return result.removed

    def cleanup_worktree(
        self,
        *,
        repo_path: Path | None,
        path: Path,
        branch: str | None,
        base_commit: str | None,
    ) -> CleanupResult:
        """Remove an AgentTape-owned worktree if (and only if) it holds no uncommitted work.

        The branch is deleted (safe ``-d``) only when it has no commits beyond the base;
        otherwise it is kept and the reason reported, so an approved commit is never lost.
        """
        spath = str(path)
        if not self.owns_path(path):
            return CleanupResult(False, "Refused: this directory is not AgentTape-owned", spath)
        if branch is not None and not is_owned_branch(branch):
            return CleanupResult(False, f"Refused: branch {branch} is not AgentTape-owned", spath)
        if repo_path is None:
            repo_path = self.main_repo_of(path)
        if repo_path is None:
            return CleanupResult(False, "Refused: the source repository cannot be found", spath)
        git = GuardedGit(repo_path, managed_root=self.root)
        if path.exists():
            actual = GuardedGit(path).run("symbolic-ref", "--quiet", "--short", "HEAD")
            actual_branch = actual.stdout.strip() if actual.returncode == 0 else None
            if branch is not None and actual_branch != branch:
                return CleanupResult(
                    False,
                    f"Refused: the worktree is on {actual_branch or 'a detached HEAD'}, "
                    f"not {branch}",
                    spath,
                    branch,
                )
            branch = branch or actual_branch
            try:
                clean, lines = is_clean(path)
            except RuntimeError as exc:
                return CleanupResult(False, f"Refused: {exc}", spath, branch)
            if not clean:
                return CleanupResult(
                    False,
                    f"Refused: {len(lines)} uncommitted change(s) would be lost. "
                    "Commit them (with approval) or copy them out first.",
                    spath,
                    branch,
                    uncommitted=[line[3:] for line in lines[:50]],
                )
            removed = git.run("worktree", "remove", spath, timeout=60)
            if removed.returncode != 0:
                return CleanupResult(
                    False,
                    "Refused by git: " + (removed.stderr or removed.stdout).strip()[:300],
                    spath,
                    branch,
                )
        else:
            git.run("worktree", "prune")
        forget_worktree_git_dir(path)
        result = CleanupResult(True, "Worktree removed", spath, branch)
        if branch and is_owned_branch(branch):
            self._maybe_delete_branch(git, branch, base_commit, result)
        return result

    def _maybe_delete_branch(
        self, git: GuardedGit, branch: str, base: str | None, result: CleanupResult
    ) -> None:
        ref = git.run("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
        if ref.returncode != 0:
            return
        if base is None:
            result.branch_kept_reason = "kept: the base commit is unknown"
            return
        ahead = git.run("rev-list", "--count", f"{base}..{branch}")
        count = int(ahead.stdout.strip() or "0") if ahead.returncode == 0 else -1
        if count != 0:
            result.branch_kept_reason = (
                f"kept: it has {count} commit(s) beyond the base"
                if count > 0
                else "kept: commits could not be counted"
            )
            return
        deleted = git.run("branch", "-d", branch)
        if deleted.returncode == 0:
            result.branch_deleted = True
        else:
            result.branch_kept_reason = "kept: " + (deleted.stderr.strip()[:200] or "git refused")

    # ------------------------------------------------------------------ reproduction snapshots

    def prepare_snapshot(self, *, key: str, repo_path: Path, commit: str) -> Path:
        """Create a detached (branchless) worktree at ``commit``, inside the managed root.

        ``key`` names the snapshot directory (a reproduction's own id, so an abandoned
        snapshot left behind by a crash is a :class:`RetentionService` orphan, exactly like
        any other leftover under this root). Detached: no branch is ever created here, so
        the V1 safety policy's "AgentTape never automatically deletes a branch" is met by
        construction - there is no branch to delete. Raises :class:`IsolationError` for a
        non-hex ``commit``, an already-existing path, or a commit that does not resolve in
        ``repo_path``; never starts ``git worktree add`` on an unresolved commit.
        """
        if not HEX_COMMIT.fullmatch(commit):
            raise IsolationError(f"{commit!r} is not a Git commit hash")
        path = self.worktree_path(key)
        if path.exists():
            raise IsolationError(f"{path} already exists")
        git = GuardedGit(repo_path, managed_root=self.root)
        verify = git.run("rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}")
        if verify.returncode != 0 or not verify.stdout.strip():
            raise IsolationError(f"{commit} does not resolve to a commit in {repo_path}")
        self.root.mkdir(parents=True, exist_ok=True)
        result = git.run("worktree", "add", "--detach", str(path), commit, timeout=120)
        if result.returncode != 0:
            raise IsolationError(
                "git worktree add --detach failed: "
                + (result.stderr or result.stdout).strip()[:500]
            )
        try:
            record_worktree_git_dir(path)
        except (OSError, RuntimeError) as exc:
            raise IsolationError(f"could not record the snapshot's git dir: {exc}") from exc
        return path

    def remove_snapshot(self, *, repo_path: Path, path: Path) -> str:
        """Remove a detached snapshot :meth:`prepare_snapshot` created. Never ``--force``.

        Refuses (returns a message, never raises) a path this manager does not own. When
        ``git worktree remove`` itself refuses (the re-run left files behind), this deletes
        only AgentTape's own snapshot directory - the pinned commit's checkout plus whatever
        the re-run wrote inside it, never agent or user work outside this directory - and
        then prunes the now-stale worktree admin entry.
        """
        if not self.owns_path(path):
            return "Refused: this directory is not AgentTape-owned"
        git = GuardedGit(repo_path, managed_root=self.root)
        if not path.exists():
            git.run("worktree", "prune")
            forget_worktree_git_dir(path)
            return "Snapshot already gone; pruned the worktree admin entry"
        removed = git.run("worktree", "remove", str(path), timeout=60)
        if removed.returncode != 0:
            shutil.rmtree(path, ignore_errors=True)
            git.run("worktree", "prune")
            forget_worktree_git_dir(path)
            detail = (removed.stderr or removed.stdout).strip()[:200]
            return f"Removed the snapshot directory directly and pruned (git refused: {detail})"
        forget_worktree_git_dir(path)
        return "Snapshot removed"

    @staticmethod
    def main_repo_of(path: Path) -> Path | None:
        """The main working tree of the repository ``path`` is a worktree of."""
        if not path.exists():
            return None
        common = GuardedGit(path).run("rev-parse", "--path-format=absolute", "--git-common-dir")
        if common.returncode != 0 or not common.stdout.strip():
            return None
        git_dir = Path(common.stdout.strip())
        return git_dir.parent if git_dir.name == ".git" else None
