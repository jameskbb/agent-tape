# ADR 0006: Git worktree isolation

- Status: Accepted
- Date: 2026-09-23

## Context

Agents edit repositories. Users need to see exactly what changed, keep their own working
tree untouched when they choose isolation, and never lose work to automatic cleanup.

## Decision

- Every run snapshots repository state at creation (`git/snapshot.py`: HEAD, branch,
  dirty files, worktree support) into its immutable snapshot. Snapshot helpers are
  read-only; they never stage, reset, or write.
- Isolation is provided by the worktree manager (`git/manager.py`): when a project
  enables `git_isolation`, a run executes in an AgentTape-owned `git worktree` under
  `<data_dir>/workspaces/`, created from the recorded base commit. The worker obtains the
  directory through the `hooks.register_workdir_preparer` extension point.
- Changes are captured as `file_changes` rows plus a diff artifact
  (`register_after_attempt` hook). Protected paths are flagged.
- Commits happen only after an approval; AgentTape never pushes, merges, force-resets, or
  deletes branches or worktrees it did not create. Cleanup of owned worktrees first
  verifies there is no uncommitted work.

## Consequences

- Without isolation, agents run in the project directory itself and the dirty-tree
  warning in the snapshot makes that visible.
- Worktrees share the object database, so creation is fast and cheap.

## Implementation notes

- `git/runner.py` (`GuardedGit`) checks every AgentTape Git call against the policy before
  a process starts; `tests/unit/test_git_policy.py` covers the rejections.
- `run_workspaces` records each run's workdir and ownership; worktrees live in
  `<data_dir>/workspaces/git/<run_id>` on branch `agenttape/run-<run_id>`.
- Cleanup refuses when `git status` shows any uncommitted or untracked file, never uses
  `--force`, and deletes the owned branch (safe `-d`) only when it has no commits beyond
  the base, so an approved commit is never lost.
- Commits use an approval of type `commit` with the `git_commit` handler; the commit is
  refused if the worktree changed after the request (fingerprint check).
