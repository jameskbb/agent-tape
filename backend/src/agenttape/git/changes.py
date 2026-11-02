"""Capture what an attempt changed in a Git working tree, relative to a base commit.

Read-only: tracked changes come from ``git diff <base>``, untracked files from
``git diff --no-index /dev/null <file>`` (so nothing is staged, not even intent-to-add).
Because Git only detects renames between tracked paths, a deleted file and a new
untracked file are paired by content here (old text from the base commit's blob), the
same 50% similarity rule Git uses. External diff drivers and text conversion filters are
disabled, so a repository's own configuration cannot run programs during capture. The
combined diff is size-capped; binary files contribute metadata only. Whole repositories
are never stored.

Each untracked file costs one Git subprocess, so at most ``MAX_UNTRACKED_DIFFED`` of them
get a hunk: an agent that writes 10,000 files into an unignored ``node_modules`` would
otherwise make capture take minutes. Every untracked file is still listed in the entries;
the ones past the cap (and any larger than ``MAX_UNTRACKED_FILE_BYTES``) are returned in
``skipped_untracked`` so the caller can record that they have no hunk.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path

from agenttape.git.diffparse import TRUNCATION_MARKER, split_by_file
from agenttape.git.runner import GuardedGit
from agenttape.git.snapshot import NumstatEntry, working_tree_changes

MAX_DIFF_BYTES = 1024 * 1024
MAX_UNTRACKED_FILE_BYTES = 256 * 1024
MAX_UNTRACKED_DIFFED = 500
DIFF_FLAGS = ("--no-color", "--no-ext-diff", "--no-textconv", "-M")
RENAME_THRESHOLD = 0.5
MAX_RENAME_CANDIDATES = 50


@dataclass
class CapturedChanges:
    base: str
    entries: list[NumstatEntry]
    diff_text: str
    truncated: bool = False
    sizes: dict[str, int] = field(default_factory=dict)
    skipped_untracked: list[str] = field(default_factory=list)
    #: Why each skipped untracked file has no hunk: ``"size_cap"`` (the file is over
    #: ``max_untracked_file_bytes``) or ``"count_cap"`` (``max_untracked_diffed`` files were
    #: already diffed), so a reviewer is never told the wrong reason.
    diff_omitted: dict[str, str] = field(default_factory=dict)


def _file_size(repo: Path, rel: str) -> int | None:
    try:
        target = (repo / rel).resolve()
        if not target.is_relative_to(repo.resolve()) or not target.is_file():
            return None
        return target.stat().st_size
    except OSError:
        return None


def similarity(old: str, new: str) -> float:
    """Line-based similarity in [0, 1]."""
    if old == new:
        return 1.0
    matcher = difflib.SequenceMatcher(None, old.splitlines(), new.splitlines(), autojunk=False)
    return matcher.ratio()


def pair_renames(
    deleted: dict[str, str], added: dict[str, str], *, threshold: float = RENAME_THRESHOLD
) -> list[tuple[str, str, float]]:
    """Greedy best-match pairing of deleted and added text files: (old, new, similarity)."""
    scores: list[tuple[float, str, str]] = []
    for old, old_text in deleted.items():
        for new, new_text in added.items():
            score = similarity(old_text, new_text)
            if score >= threshold:
                # Prefer the same file name on ties (a move rather than a rewrite).
                bonus = 0.001 if Path(old).name == Path(new).name else 0.0
                scores.append((score + bonus, old, new))
    pairs: list[tuple[str, str, float]] = []
    used_old: set[str] = set()
    used_new: set[str] = set()
    for score, old, new in sorted(scores, key=lambda t: (-t[0], t[1], t[2])):
        if old in used_old or new in used_new:
            continue
        used_old.add(old)
        used_new.add(new)
        pairs.append((old, new, min(score, 1.0)))
    return pairs


def rename_diff(
    old: str, new: str, old_text: str, new_text: str, score: float
) -> tuple[str, int, int]:
    """A Git-style rename diff plus (insertions, deletions)."""
    lines = list(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{old}",
            tofile=f"b/{new}",
            n=3,
        )
    )
    ins = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
    dels = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
    header = (
        f"diff --git a/{old} b/{new}\n"
        f"similarity index {int(score * 100)}%\n"
        f"rename from {old}\nrename to {new}\n"
    )
    body = "".join(
        ln if ln.endswith("\n") else ln + "\n\\ No newline at end of file\n" for ln in lines
    )
    return header + body, ins, dels


def _read_text(path: Path, cap: int) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > cap:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def _detect_renames(
    git: GuardedGit,
    repo: Path,
    base: str,
    entries: list[NumstatEntry],
    untracked: set[str],
    cap: int,
) -> dict[str, tuple[str, float, str, int, int]]:
    """new path -> (old path, similarity, diff text, insertions, deletions)."""
    deleted = [e for e in entries if e.change_type == "deleted" and not e.is_binary]
    added = [e for e in entries if e.path in untracked and not e.is_binary]
    if not deleted or not added:
        return {}
    old_texts: dict[str, str] = {}
    for e in deleted[:MAX_RENAME_CANDIDATES]:
        blob = git.run("cat-file", "-p", f"{base}:{e.path}")
        if blob.returncode == 0 and len(blob.stdout) <= cap:
            old_texts[e.path] = blob.stdout
    new_texts: dict[str, str] = {}
    for e in added[:MAX_RENAME_CANDIDATES]:
        text = _read_text(repo / e.path, cap)
        if text is not None:
            new_texts[e.path] = text
    renamed: dict[str, tuple[str, float, str, int, int]] = {}
    for old, new, score in pair_renames(old_texts, new_texts):
        diff, ins, dels = rename_diff(old, new, old_texts[old], new_texts[new], score)
        renamed[new] = (old, score, diff, ins, dels)
    return renamed


def capture_changes(
    repo: Path,
    base: str,
    *,
    max_diff_bytes: int = MAX_DIFF_BYTES,
    max_untracked_file_bytes: int = MAX_UNTRACKED_FILE_BYTES,
    max_untracked_diffed: int = MAX_UNTRACKED_DIFFED,
) -> CapturedChanges:
    git = GuardedGit(repo)
    entries = working_tree_changes(repo, base)
    tracked_chunks = split_by_file(git.run("diff", *DIFF_FLAGS, base, "--").stdout)
    changed_paths = {e.path for e in entries}
    untracked_raw = git.run("ls-files", "--others", "--exclude-standard", "-z").stdout
    untracked = {p for p in untracked_raw.split("\0") if p and p in changed_paths}
    sizes: dict[str, int] = {}
    for e in entries:
        size = _file_size(repo, e.path)
        if size is not None:
            sizes[e.path] = size

    renamed = _detect_renames(git, repo, base, entries, untracked, max_untracked_file_bytes)
    olds = {v[0] for v in renamed.values()}
    if renamed:
        merged: list[NumstatEntry] = []
        for e in entries:
            if e.path in olds and e.change_type == "deleted":
                continue
            if e.path in renamed:
                old, _score, _diff, ins, dels = renamed[e.path]
                merged.append(NumstatEntry(e.path, "renamed", ins, dels, False, old_path=old))
                continue
            merged.append(e)
        entries = merged

    parts = [chunk for path, chunk in tracked_chunks.items() if path not in olds]
    skipped: list[str] = []
    omitted: dict[str, str] = {}
    diffed = 0
    for e in entries:
        if e.path in renamed:
            parts.append(renamed[e.path][2])
            continue
        if e.path not in untracked:
            continue
        size = sizes.get(e.path)
        if size is not None and size > max_untracked_file_bytes:
            omitted[e.path] = "size_cap"
        elif diffed >= max_untracked_diffed:
            omitted[e.path] = "count_cap"
        if e.path in omitted:
            skipped.append(e.path)
            continue
        diffed += 1
        result = git.run("diff", *DIFF_FLAGS, "--no-index", "--", "/dev/null", e.path)
        parts.append(result.stdout)
    text = "".join(p if p.endswith("\n") or not p else p + "\n" for p in parts)
    truncated = False
    encoded = text.encode("utf-8")
    if len(encoded) > max_diff_bytes:
        text = encoded[:max_diff_bytes].decode("utf-8", errors="ignore")
        text = text[: text.rfind("\n") + 1] if "\n" in text else ""
        text += TRUNCATION_MARKER + "\n"
        truncated = True
    return CapturedChanges(
        base=base,
        entries=entries,
        diff_text=text,
        truncated=truncated,
        sizes=sizes,
        skipped_untracked=skipped,
        diff_omitted=omitted,
    )


def head_commit(repo: Path) -> str | None:
    result = GuardedGit(repo).run("rev-parse", "--verify", "--quiet", "HEAD")
    return (result.stdout.strip() or None) if result.returncode == 0 else None


def is_clean(repo: Path) -> tuple[bool, list[str]]:
    """(clean, porcelain lines) including untracked files; ignored files do not count."""
    result = GuardedGit(repo).run("status", "--porcelain=v1", "--untracked-files=all")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git status failed")
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return not lines, lines
