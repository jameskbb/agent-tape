"""Parse ``git diff`` unified output into files, hunks, and numbered lines.

Pure and tolerant: unknown header lines are kept as metadata, a truncated diff (the
capture cap) ends cleanly, and binary files are reported without content.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

LineKind = Literal["add", "del", "ctx", "note"]

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")
DIFF_GIT_RE = re.compile(r'^diff --git (?:"?a/(.+?)"?) (?:"?b/(.+?)"?)$')
TRUNCATION_MARKER = "[agenttape: diff truncated at size cap]"


@dataclass
class DiffLine:
    kind: LineKind
    text: str
    old: int | None = None
    new: int | None = None


@dataclass
class DiffHunk:
    header: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    section: str = ""
    lines: list[DiffLine] = field(default_factory=list)


@dataclass
class DiffFile:
    path: str
    old_path: str | None = None
    change_type: str = "modified"
    is_binary: bool = False
    old_mode: str | None = None
    new_mode: str | None = None
    similarity: int | None = None
    insertions: int = 0
    deletions: int = 0
    hunks: list[DiffHunk] = field(default_factory=list)
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_ESCAPES = {"\\\\": "\\", '\\"': '"', "\\t": "\t", "\\n": "\n"}


def _unquote(path: str) -> str:
    """Undo Git's C-style quoting of unusual paths (``core.quotepath=false`` keeps UTF-8)."""
    if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
        path = path[1:-1]
        path = re.sub(r'\\[\\"tn]', lambda m: _ESCAPES[m.group(0)], path)
    return path


def _strip_prefix(path: str) -> str | None:
    path = _unquote(path.strip())
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def parse_unified_diff(text: str, *, max_lines_per_file: int = 5000) -> list[DiffFile]:
    """Split a multi-file unified diff into :class:`DiffFile` objects."""
    files: list[DiffFile] = []
    current: DiffFile | None = None
    hunk: DiffHunk | None = None
    old_no = new_no = 0
    line_count = 0

    def start(path: str, old_path: str | None) -> DiffFile:
        nonlocal hunk, line_count
        hunk = None
        line_count = 0
        f = DiffFile(path=path, old_path=old_path)
        files.append(f)
        return f

    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            m = DIFF_GIT_RE.match(raw)
            if m:
                old, new = m.group(1), m.group(2)
            else:
                parts = raw[len("diff --git ") :].split(" b/", 1)
                old = parts[0][2:] if parts[0].startswith("a/") else parts[0]
                new = parts[1] if len(parts) > 1 else old
            current = start(_unquote(new), _unquote(old) if old != new else None)
            continue
        if raw.startswith(TRUNCATION_MARKER):
            if current is not None:
                current.truncated = True
            break
        if current is None:
            continue
        if hunk is None or not raw or raw[0] not in " +-\\":
            # File header area (or a new hunk header).
            if raw.startswith("@@"):
                m = HUNK_RE.match(raw)
                if not m:
                    continue
                hunk = DiffHunk(
                    header=raw,
                    old_start=int(m.group(1)),
                    old_count=int(m.group(2)) if m.group(2) is not None else 1,
                    new_start=int(m.group(3)),
                    new_count=int(m.group(4)) if m.group(4) is not None else 1,
                    section=m.group(5) or "",
                )
                current.hunks.append(hunk)
                old_no, new_no = hunk.old_start, hunk.new_start
                continue
            if hunk is not None and not raw:
                # A blank context line inside a hunk (some tools strip the leading space).
                pass
            else:
                _header(current, raw)
                continue
        # Inside a hunk.
        if line_count >= max_lines_per_file:
            current.truncated = True
            if raw.startswith("+") and not raw.startswith("+++"):
                current.insertions += 1
            elif raw.startswith("-") and not raw.startswith("---"):
                current.deletions += 1
            continue
        tag = raw[:1]
        body = raw[1:]
        if tag == "+":
            hunk.lines.append(DiffLine("add", body, None, new_no))
            new_no += 1
            current.insertions += 1
        elif tag == "-":
            hunk.lines.append(DiffLine("del", body, old_no, None))
            old_no += 1
            current.deletions += 1
        elif tag == "\\":
            hunk.lines.append(DiffLine("note", body.strip()))
        else:
            hunk.lines.append(DiffLine("ctx", body, old_no, new_no))
            old_no += 1
            new_no += 1
        line_count += 1
    return files


def _header(f: DiffFile, raw: str) -> None:
    if raw.startswith("new file mode"):
        f.change_type = "added"
        f.new_mode = raw.rsplit(" ", 1)[-1]
    elif raw.startswith("deleted file mode"):
        f.change_type = "deleted"
        f.old_mode = raw.rsplit(" ", 1)[-1]
    elif raw.startswith("rename from "):
        f.old_path = _unquote(raw[len("rename from ") :])
        f.change_type = "renamed"
    elif raw.startswith("rename to "):
        f.path = _unquote(raw[len("rename to ") :])
        f.change_type = "renamed"
    elif raw.startswith("similarity index "):
        try:
            f.similarity = int(raw[len("similarity index ") :].rstrip("%"))
        except ValueError:
            f.similarity = None
    elif raw.startswith("old mode "):
        f.old_mode = raw[len("old mode ") :]
    elif raw.startswith("new mode "):
        f.new_mode = raw[len("new mode ") :]
    elif raw.startswith("Binary files ") or raw.startswith("GIT binary patch"):
        f.is_binary = True
    elif raw.startswith("--- "):
        old = _strip_prefix(raw[4:])
        if old is None:
            f.change_type = "added"
    elif raw.startswith("+++ "):
        new = _strip_prefix(raw[4:])
        if new is None:
            f.change_type = "deleted"
        elif f.change_type != "renamed":
            f.path = new


def split_by_file(text: str) -> dict[str, str]:
    """Raw diff text per file path (for size-capped per-file downloads)."""
    chunks: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for raw in text.splitlines(keepends=True):
        if raw.startswith("diff --git "):
            if current is not None:
                chunks[current] = "".join(buf)
            m = DIFF_GIT_RE.match(raw.rstrip("\n"))
            current = _unquote(m.group(2)) if m else raw.strip()
            buf = [raw]
        elif current is not None:
            buf.append(raw)
    if current is not None:
        chunks[current] = "".join(buf)
    return chunks
