"""Read-only Git helpers: repository state snapshots and working-tree change summaries.

All commands use argument arrays (never a shell) with a short timeout. Nothing here
mutates a repository. Isolated worktrees live in :mod:`agenttape.git.manager`.
"""

from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agenttape.git.invoke import run_git_hardened

GIT_TIMEOUT_SECONDS = 10.0


def run_git(
    repo: Path, *args: str, timeout: float = GIT_TIMEOUT_SECONDS, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a read-only Git command through the hardened invoker (no shell, no repo programs)."""
    return run_git_hardened(repo, *args, timeout=timeout, input_text=stdin)


@dataclass
class GitState:
    is_repo: bool
    head: str | None = None
    branch: str | None = None
    dirty: bool = False
    dirty_files: list[str] = field(default_factory=list)
    dirty_count: int = 0
    worktree_supported: bool = False
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


MAX_DIRTY_LISTED = 50


def capture_git_state(path: str | Path) -> GitState:
    """Snapshot HEAD, branch, and dirty files for ``path``. Never raises."""
    repo = Path(path)
    if not repo.is_dir():
        return GitState(is_repo=False, error="path is not a directory")
    try:
        inside = run_git(repo, "rev-parse", "--is-inside-work-tree")
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return GitState(is_repo=False)
        head = run_git(repo, "rev-parse", "--verify", "--quiet", "HEAD")
        branch = run_git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
        status = run_git(repo, "status", "--porcelain=v1", "--untracked-files=normal")
        dirty = [line[3:] for line in status.stdout.splitlines() if line.strip()]
        version = run_git(repo, "worktree", "list", "--porcelain")
        return GitState(
            is_repo=True,
            head=head.stdout.strip() or None,
            branch=branch.stdout.strip() or None,
            dirty=bool(dirty),
            dirty_files=dirty[:MAX_DIRTY_LISTED],
            dirty_count=len(dirty),
            worktree_supported=version.returncode == 0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return GitState(is_repo=False, error=str(exc))


@dataclass
class NumstatEntry:
    path: str
    change_type: str
    insertions: int | None
    deletions: int | None
    is_binary: bool
    old_path: str | None = None


def _count_untracked(path: Path, max_bytes: int = 2 * 1024 * 1024) -> tuple[int | None, bool, int]:
    """(line count or None, is_binary, size) for an untracked file, without reading huge files."""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            head = fh.read(min(size, max_bytes))
    except OSError:
        return None, False, 0
    if b"\0" in head[:8192]:
        return None, True, size
    if size > max_bytes:
        return None, False, size
    return head.count(b"\n") + (0 if head.endswith(b"\n") or not head else 1), False, size


def working_tree_changes(repo: Path, base: str = "HEAD") -> list[NumstatEntry]:
    """Changes in the working tree (tracked and untracked) relative to ``base``. Read-only."""
    names = run_git(repo, "diff", "--name-status", "-M", "-z", base)
    numstat = run_git(repo, "diff", "--numstat", "-M", "-z", base)
    counts: dict[str, tuple[int | None, int | None, bool]] = {}
    parts = numstat.stdout.split("\0")
    i = 0
    while i < len(parts):
        rec = parts[i]
        if not rec:
            i += 1
            continue
        fields = rec.split("\t")
        if len(fields) == 3 and fields[2] == "":
            # rename record: "<ins>\t<del>\t" followed by old\0new\0
            ins, dels = fields[0], fields[1]
            new_path = parts[i + 2] if i + 2 < len(parts) else ""
            binary = ins == "-"
            counts[new_path] = (None if binary else int(ins), None if binary else int(dels), binary)
            i += 3
            continue
        ins, dels, path = fields[0], fields[1], fields[2]
        binary = ins == "-"
        counts[path] = (None if binary else int(ins), None if binary else int(dels), binary)
        i += 1
    entries: list[NumstatEntry] = []
    tokens = names.stdout.split("\0")
    j = 0
    kinds = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed", "T": "modified"}
    while j < len(tokens):
        status = tokens[j]
        if not status:
            j += 1
            continue
        code = status[0]
        if code == "R" and j + 2 < len(tokens):
            old, new = tokens[j + 1], tokens[j + 2]
            n_ins, n_dels, is_bin = counts.get(new, (None, None, False))
            entries.append(NumstatEntry(new, "renamed", n_ins, n_dels, is_bin, old_path=old))
            j += 3
            continue
        path = tokens[j + 1] if j + 1 < len(tokens) else ""
        n_ins, n_dels, is_bin = counts.get(path, (None, None, False))
        entries.append(NumstatEntry(path, kinds.get(code, "modified"), n_ins, n_dels, is_bin))
        j += 2
    untracked = run_git(repo, "ls-files", "--others", "--exclude-standard", "-z")
    for rel in (p for p in untracked.stdout.split("\0") if p):
        lines, binary, _size = _count_untracked(repo / rel)
        entries.append(NumstatEntry(rel, "added", lines, 0 if lines is not None else None, binary))
    return entries


def unified_diff(repo: Path, base: str = "HEAD", max_bytes: int = 1024 * 1024) -> str:
    result = run_git(repo, "diff", "--no-color", "-M", base)
    text = result.stdout
    if len(text.encode("utf-8")) > max_bytes:
        text = text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
        text += "\n[agenttape: diff truncated at size cap]\n"
    return text
