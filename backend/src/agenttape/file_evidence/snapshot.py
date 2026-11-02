"""Start and end snapshots of the evidence boundary (read-only; argument arrays only).

Start: the commit, branch, dirty state, and a ``{path: [hash, size, binary]}`` map of
every tracked file (Git's own blob ids from ``git ls-files -s`` - no file is read) plus
every untracked, non-ignored file (hashed from disk, size-capped). Ignored entries are
listed without recursing (``--directory``), so ``node_modules/`` costs one line.

End: only the paths that matter - those with evidence, those Git reports as changed
against the starting commit, and untracked files - hashed from disk, plus numstat
(insertions/deletions) and which of them Git ignores.

Without Git (a scratch directory) both snapshots walk the tree, bounded, skipping the
noise policy's collapsed directories.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from agenttape.file_evidence.ledger import ABSENT, NoisePolicy, hash_file
from agenttape.git.snapshot import run_git, working_tree_changes

GIT_TIMEOUT = 30.0
MAX_UNTRACKED_HASHED = 5000
MAX_IGNORED_LISTED = 5000
MAX_WALK_FILES = 20000


def _git(root: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return run_git(root, *args, timeout=GIT_TIMEOUT, stdin=stdin)


def is_git_repo(root: Path) -> bool:
    try:
        out = _git(root, "rev-parse", "--is-inside-work-tree")
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and out.stdout.strip() == "true"


def _split(out: str) -> list[str]:
    return [p for p in out.split("\0") if p]


def _walk(root: Path, noise: NoisePolicy) -> tuple[dict[str, list[Any]], bool]:
    files: dict[str, list[Any]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        dirnames[:] = sorted(
            d
            for d in dirnames
            if not noise.is_git_internal(f"{rel_dir}/{d}".lstrip("/"))
            and noise.collapse_bucket(f"{rel_dir}/{d}/x".lstrip("/")) is None
        )
        for name in sorted(filenames):
            rel = f"{rel_dir}/{name}".lstrip("/")
            if noise.is_git_internal(rel) or noise.collapse_bucket(rel) is not None:
                continue
            if len(files) >= MAX_WALK_FILES:
                return files, True
            state = hash_file(root / rel)
            if state is not None:
                files[rel] = state.as_list()
    return files, False


def start_snapshot(root: Path, noise: NoisePolicy) -> dict[str, Any]:
    """The starting state of ``root``. Never raises for expected conditions."""
    if not root.is_dir():
        return {"is_repo": False, "files": {}, "details": {"error": "workdir missing"}}
    if not is_git_repo(root):
        walked, truncated = _walk(root, noise)
        return {"is_repo": False, "files": walked, "details": {"truncated": truncated}}
    head = _git(root, "rev-parse", "--verify", "--quiet", "HEAD").stdout.strip() or None
    branch = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip() or None
    files: dict[str, list[Any]] = {}
    staged = _git(root, "ls-files", "-s", "-z")
    for rec in _split(staged.stdout):
        meta, _, path = rec.partition("\t")
        fields = meta.split()
        if len(fields) >= 2 and not noise.is_git_internal(path):
            files[path] = [fields[1], None, None]
    # Sizes are one stat per tracked file (no reads).
    for path, entry in files.items():
        try:
            entry[1] = os.lstat(root / path).st_size
        except OSError:
            files[path] = list(ABSENT)
    status = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    dirty: list[str] = []
    recs = _split(status.stdout)
    i = 0
    while i < len(recs):
        rec = recs[i]
        code, path = rec[:2], rec[3:]
        if code[0] in "RC":
            i += 1  # the rename source follows
        dirty.append(path)
        i += 1
    untracked_hashed = 0
    truncated = False
    for path in dirty:
        if noise.is_git_internal(path) or noise.collapse_bucket(path) is not None:
            continue
        if untracked_hashed >= MAX_UNTRACKED_HASHED:
            truncated = True
            break
        state = hash_file(root / path)
        files[path] = state.as_list() if state is not None else list(ABSENT)
        untracked_hashed += 1
    ignored_out = _git(
        root, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z"
    )
    ignored = _split(ignored_out.stdout)
    return {
        "is_repo": True,
        "head": head,
        "branch": branch,
        "dirty": bool(dirty),
        "files": files,
        "details": {
            "dirty_count": len(dirty),
            "dirty_files": dirty[:50],
            "ignored": ignored[:MAX_IGNORED_LISTED],
            "ignored_truncated": len(ignored) > MAX_IGNORED_LISTED,
            "truncated": truncated,
        },
    }


def end_snapshot(
    root: Path,
    noise: NoisePolicy,
    *,
    base: str | None,
    evidence_paths: Iterable[str],
    start_files: dict[str, list[Any]] | None = None,
    start_dirty: Iterable[str] = (),
) -> dict[str, Any]:
    """The ending state of every path that matters for reconciliation."""
    candidates = {p for p in evidence_paths if p and not p.startswith("/")}
    if not root.is_dir():
        return {
            "is_repo": False,
            "files": {p: list(ABSENT) for p in sorted(candidates)},
            "details": {"error": "workdir missing"},
        }
    if not is_git_repo(root):
        walked, truncated = _walk(root, noise)
        for p in candidates:
            walked.setdefault(p, list(ABSENT))
        # Paths that existed at start and vanished.
        for p in start_files or {}:
            walked.setdefault(p, list(ABSENT))
        return {"is_repo": False, "files": walked, "details": {"truncated": truncated}}
    head = _git(root, "rev-parse", "--verify", "--quiet", "HEAD").stdout.strip() or None
    branch = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip() or None
    changed: set[str] = set()
    if base:
        diff = _git(root, "diff", "--name-only", "--no-renames", "-z", base)
        changed |= set(_split(diff.stdout))
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
    changed |= {p for p in _split(untracked.stdout)}
    # Files dirty at start (tracked edits or untracked files) may have been reverted.
    changed |= set(start_dirty)
    changed = {
        p for p in changed if not noise.is_git_internal(p) and noise.collapse_bucket(p) is None
    }
    candidates |= changed
    files: dict[str, list[Any]] = {}
    for p in sorted(candidates):
        state = hash_file(root / p)
        files[p] = state.as_list() if state is not None else list(ABSENT)
    ignored: list[str] = []
    if candidates:
        check = _git(root, "check-ignore", "--stdin", "-z", stdin="\0".join(sorted(candidates)))
        ignored = sorted(_split(check.stdout))
    numstat: dict[str, list[Any]] = {}
    if base:
        for e in working_tree_changes(root, base):
            numstat[e.path] = [e.insertions, e.deletions, e.is_binary]
    status = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=normal")
    return {
        "is_repo": True,
        "head": head,
        "branch": branch,
        "dirty": bool(_split(status.stdout)),
        "files": files,
        "details": {
            "base": base,
            "changed_vs_base": sorted(changed)[:2000],
            "numstat": numstat,
            "ignored": ignored,
        },
    }
