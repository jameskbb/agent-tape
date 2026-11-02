"""Pure File Evidence logic: noise policy, hashing, telemetry mapping, the file ledger.

Nothing here touches the database or the event loop, so every rule is unit-testable:

* :class:`NoisePolicy` - what is excluded (``.git``), collapsed (dependency, cache, build,
  coverage, AgentTape runtime directories, user globs), or captured individually;
* :func:`telemetry_evidence` - one ``events`` row -> evidence drafts (reads, searches,
  writes, renames, tool calls, command references), never guessing a read;
* :func:`build_summaries` - evidence + start/end snapshots -> one summary per logical
  file (rename chains merged), with the classifications and labels of
  ``docs/FILE-EVIDENCE.md``;
* :func:`run_completeness` - the honest evidence completeness statement for a run.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import posixpath
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from agenttape.domain.policy import matches_protected
from agenttape.domain.shell_reads import classify_shell_read

# --------------------------------------------------------------------------- vocabulary

OPERATIONS = ("read", "search", "reference", "create", "modify", "delete", "rename", "collapsed")
WRITE_OPS = frozenset({"create", "modify", "delete", "rename"})
SOURCES = (
    "agent_hook",
    "tool_event",
    "filesystem_watcher",
    "git_start",
    "git_end",
    "command_reference",
    "reconciliation",
    "manual",
)
#: Final classifications and their user-facing labels (docs/FILE-EVIDENCE.md).
LABELS: dict[str, str] = {
    "observed_read": "Observed read",
    "inferred_access": "Inferred access",
    "created": "Created",
    "modified": "Modified",
    "renamed": "Renamed",
    "deleted": "Deleted",
    "restored": "Restored",
    "net_unchanged": "Net unchanged",
    "generated": "Generated",
    "outside_boundary": "Outside boundary attempt",
    "unknown": "Unknown",
}
NET_STATUSES = ("added", "modified", "deleted", "renamed", "unchanged", "unknown", "pending")
#: Command categories whose files count as "generated" (demo agent and hooks may declare).
GENERATING_CATEGORIES = frozenset({"formatter", "build", "test", "install", "generate"})
#: Watcher timestamps are taken when a debounced batch is processed, so a batch caused by a
#: command can be stamped slightly after the command finished.
COMMAND_WINDOW_SLACK_SECONDS = 2.0
MAX_HASH_BYTES = 8 * 1024 * 1024
BINARY_SNIFF_BYTES = 8192

#: Directory names collapsed by default (at any depth).
DEFAULT_COLLAPSED_DIRS = (
    "node_modules",
    "bower_components",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    ".cache",
    ".gradle",
    ".next",
    ".nuxt",
    ".turbo",
    ".parcel-cache",
    "build",
    "dist",
    "target",
    "coverage",
    "htmlcov",
    ".nyc_output",
    ".agenttape",
)
#: File-name globs collapsed by default (bucketed under their directory).
DEFAULT_COLLAPSED_FILES = ("*.pyc", "*.pyo", ".coverage", ".coverage.*", ".DS_Store")
#: Temporary-save patterns: a temp file created and removed in one batch next to its target
#: is folded into the target's history instead of being listed on its own.
TEMP_SAVE_PATTERNS = ("*.tmp", "*~", ".*.swp", ".*.swx", "#*#", ".#*", "*.tmp.*")


@dataclass(frozen=True)
class NoisePolicy:
    collapsed_dirs: tuple[str, ...] = DEFAULT_COLLAPSED_DIRS
    collapsed_files: tuple[str, ...] = DEFAULT_COLLAPSED_FILES
    #: Extra user globs to collapse (``settings.file_evidence.collapse``).
    collapse_globs: tuple[str, ...] = ()
    #: Globs always captured individually, overriding the defaults (``...capture``).
    capture_globs: tuple[str, ...] = ()

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any] | None) -> NoisePolicy:
        fe = dict((settings or {}).get("file_evidence") or {})
        collapse = tuple(str(g) for g in fe.get("collapse") or [] if isinstance(g, str))
        capture = tuple(str(g) for g in fe.get("capture") or [] if isinstance(g, str))
        return cls(collapse_globs=collapse, capture_globs=capture)

    def as_dict(self) -> dict[str, Any]:
        return {
            "excluded": [".git"],
            "collapsed_dirs": list(self.collapsed_dirs),
            "collapsed_files": list(self.collapsed_files),
            "collapse_globs": list(self.collapse_globs),
            "capture_globs": list(self.capture_globs),
        }

    @staticmethod
    def is_git_internal(rel: str) -> bool:
        return rel == ".git" or rel.startswith(".git/")

    def collapse_bucket(self, rel: str) -> str | None:
        """The collapsed directory bucket for ``rel`` (e.g. ``web/node_modules/``) or None."""
        if any(fnmatch.fnmatchcase(rel, g) for g in self.capture_globs):
            return None
        parts = rel.split("/")
        for i, part in enumerate(parts[:-1]):
            if part in self.collapsed_dirs:
                return "/".join(parts[: i + 1]) + "/"
        name = parts[-1]
        if name in self.collapsed_dirs:  # the directory entry itself
            return rel + "/"
        if any(fnmatch.fnmatchcase(name, g) for g in self.collapsed_files):
            parent = "/".join(parts[:-1])
            return (parent + "/" if parent else "") + "*" + PurePosixPath(name).suffix
        for glob in self.collapse_globs:
            if fnmatch.fnmatchcase(rel, glob) or fnmatch.fnmatchcase(rel, glob.rstrip("/") + "/*"):
                return glob if glob.endswith("/") else glob + ("" if "*" in glob else "/")
        return None


def is_temp_save(name: str) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in TEMP_SAVE_PATTERNS)


# --------------------------------------------------------------------------- hashing


def blob_hash(data: bytes) -> str:
    """Git's blob object id for ``data`` (so start hashes can come from ``git ls-files -s``)."""
    h = hashlib.sha1(usedforsecurity=False)
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


@dataclass(frozen=True)
class FileState:
    hash: str | None
    size: int | None
    binary: bool | None

    def as_list(self) -> list[Any]:
        return [self.hash, self.size, self.binary]

    @classmethod
    def from_list(cls, value: Sequence[Any] | None) -> FileState | None:
        if not value:
            return None
        hash_, size, binary = [*list(value), None, None, None][:3]
        if hash_ is None and size is None:
            return None  # recorded as absent
        return cls(hash_, size, binary)


ABSENT: list[Any] = [None, None, None]


def hash_file(path: Path, max_bytes: int = MAX_HASH_BYTES) -> FileState | None:
    """Stat and hash a regular file (git blob id). None when it does not exist.

    Files above ``max_bytes`` get a size but no hash. Symlinks hash their target text
    (like Git). Contents are never kept.
    """
    try:
        st = os.lstat(path)
    except OSError:
        return None
    import stat as _stat

    if _stat.S_ISLNK(st.st_mode):
        try:
            target = str(path.readlink()).encode()
        except OSError:
            return FileState(None, None, None)
        return FileState(blob_hash(target), len(target), False)
    if not _stat.S_ISREG(st.st_mode):
        return None
    if st.st_size > max_bytes:
        try:
            with path.open("rb") as fh:
                head = fh.read(BINARY_SNIFF_BYTES)
        except OSError:
            return FileState(None, st.st_size, None)
        return FileState(None, st.st_size, b"\0" in head)
    try:
        data = path.read_bytes()
    except OSError:
        return FileState(None, st.st_size, None)
    return FileState(blob_hash(data), len(data), b"\0" in data[:BINARY_SNIFF_BYTES])


# --------------------------------------------------------------------------- paths


@dataclass(frozen=True)
class ResolvedPath:
    path: str  #: repo-relative when inside, else the normalized declared path
    inside: bool


def resolve_path(raw: str, root: Path, cwd: str | None = None) -> ResolvedPath | None:
    """Normalize a declared path against the evidence boundary ``root``.

    Never touches the file (no symlink resolution of paths outside the root, no reads):
    ``..`` segments are collapsed lexically, and the result is inside only when it stays
    under ``root``. Returns None for empty or non-path values.
    """
    text = str(raw or "").strip()
    if not text or "\x00" in text or len(text) > 4096:
        return None
    text = text.replace("\\", "/")
    root_s = root.as_posix().rstrip("/")
    if text.startswith("~"):
        return ResolvedPath(posixpath.normpath(text), False)
    if posixpath.isabs(text):
        absolute = posixpath.normpath(text)
    else:
        base = root_s
        if cwd:
            c = cwd.replace("\\", "/")
            base = posixpath.normpath(c if posixpath.isabs(c) else posixpath.join(root_s, c))
        absolute = posixpath.normpath(posixpath.join(base, text))
    if absolute == root_s:
        return ResolvedPath(".", True)
    if absolute.startswith(root_s + "/"):
        return ResolvedPath(absolute[len(root_s) + 1 :], True)
    return ResolvedPath(absolute, False)


# --------------------------------------------------------------------------- evidence drafts


@dataclass
class EvidenceDraft:
    operation: str
    path: str
    source: str
    quality: str
    ts: datetime
    dedupe_key: str
    inside_boundary: bool = True
    previous_path: str | None = None
    last_ts: datetime | None = None
    count: int = 1
    attempt_id: str | None = None
    node_key: str | None = None
    adapter: str | None = None
    runtime: str | None = None
    tool: str | None = None
    command: str | None = None
    correlation_id: str | None = None
    source_event_id: str | None = None
    before_hash: str | None = None
    after_hash: str | None = None
    size_bytes: int | None = None
    is_binary: bool | None = None
    protected_patterns: list[str] = field(default_factory=list)
    policy: dict[str, Any] | None = None
    details: dict[str, Any] = field(default_factory=dict)


_WRITE_OPERATION = {
    "create": "create",
    "add": "create",
    "added": "create",
    "new": "create",
    "modify": "modify",
    "modified": "modify",
    "edit": "modify",
    "update": "modify",
    "append": "modify",
    "write": "modify",
    "patch": "modify",
    "delete": "delete",
    "deleted": "delete",
    "remove": "delete",
    "rename": "rename",
    "move": "rename",
}
#: Tool names (Claude Code and similar) -> operation, for ``tool_call`` events.
_TOOL_OPS = {
    "read": "read",
    "notebookread": "read",
    "read_file": "read",
    "view": "read",
    "write": "modify",
    "write_file": "modify",
    "edit": "modify",
    "multiedit": "modify",
    "notebookedit": "modify",
    "str_replace_editor": "modify",
    "apply_patch": "modify",
    "glob": "search",
    "grep": "search",
    "ls": "search",
    "list_files": "search",
}
_PATH_KEYS = ("file_path", "path", "notebook_path", "filename", "target_file")


def is_read_call(payload: Mapping[str, Any]) -> bool:
    """True when File Evidence can record a ``tool_call`` with this payload as a read: a
    read tool (``Read``, ``NotebookRead``, ...) whose input names a path, or a ``Bash``
    command classified as a shell read. ``EventRecorder`` uses it to decide whose
    ``tool_result`` output is kept for verification beyond the event text cap
    (``findings/retained_output.py``). A shell read's paths are not resolved here, so one
    whose paths turn out not to be files is still kept (and never cited)."""
    tool = str(payload.get("tool") or "")
    args = payload.get("input")
    if not isinstance(args, dict):
        return False
    if _TOOL_OPS.get(tool.lower().replace("-", "_")) == "read":
        return any(isinstance(args.get(k), str) and args.get(k) for k in _PATH_KEYS)
    if tool.lower() == "bash":
        return classify_shell_read(args.get("command")) is not None
    return False


@dataclass(frozen=True)
class EventRow:
    """The parts of an ``events`` row the telemetry mapper needs."""

    id: str
    sequence: int
    ts: datetime
    type: str
    source: str
    payload: Mapping[str, Any]
    attempt_id: str | None = None
    node_key: str | None = None


def telemetry_evidence(
    ev: EventRow,
    root: Path,
    *,
    is_file: Callable[[str], bool] | None = None,
    runtime: str | None = None,
) -> list[EvidenceDraft]:
    """Evidence drafts for one event. Only explicit agent/tool reports become reads."""
    if ev.source == "agenttape":
        return []
    payload = ev.payload or {}
    out: list[EvidenceDraft] = []

    def add(
        op: str,
        raw_path: Any,
        source: str,
        quality: str,
        *,
        previous: Any = None,
        tool: str | None = None,
        command: str | None = None,
        correlation: str | None = None,
        cwd: str | None = None,
        details: dict[str, Any] | None = None,
        size: Any = None,
    ) -> None:
        resolved = resolve_path(str(raw_path or ""), root, cwd)
        if resolved is None or resolved.path == ".":
            return
        prev = resolve_path(str(previous), root, cwd) if previous else None
        out.append(
            EvidenceDraft(
                operation=op,
                path=resolved.path,
                inside_boundary=resolved.inside and (prev is None or prev.inside),
                previous_path=prev.path if prev else None,
                source=source,
                quality=quality,
                ts=ev.ts,
                dedupe_key=f"event:{ev.id}:{len(out)}",
                attempt_id=ev.attempt_id,
                node_key=ev.node_key,
                adapter=ev.source,
                runtime=runtime,
                tool=tool,
                command=command,
                correlation_id=correlation,
                source_event_id=ev.id,
                size_bytes=size if isinstance(size, int) else None,
                details=details or {},
            )
        )

    etype = ev.type
    if etype == "file_read":
        operation = str(payload.get("operation") or "read")
        if operation == "search":
            paths = payload.get("paths") if isinstance(payload.get("paths"), list) else []
            for p in (paths or [])[:200]:
                add(
                    "search",
                    p,
                    "agent_hook",
                    "inferred",
                    details={"query": str(payload.get("query") or "")[:300]},
                )
            return out
        add("read", payload.get("path"), "agent_hook", "reported", size=payload.get("bytes"))
        return out
    if etype == "file_write":
        operation = _WRITE_OPERATION.get(str(payload.get("operation") or "write").lower(), "modify")
        add(
            operation,
            payload.get("path"),
            "agent_hook",
            "reported",
            previous=payload.get("old_path") if operation == "rename" else None,
            size=payload.get("bytes"),
            details={"reported_operation": payload.get("operation")},
        )
        return out
    if etype == "tool_call":
        tool = str(payload.get("tool") or "")
        args = payload.get("input") if isinstance(payload.get("input"), dict) else {}
        assert isinstance(args, dict)
        op = _TOOL_OPS.get(tool.lower().replace("-", "_"))
        raw = next((args.get(k) for k in _PATH_KEYS if isinstance(args.get(k), str)), None)
        correlation = str(payload.get("tool_use_id") or "") or None
        if tool.lower() == "bash":
            read = classify_shell_read(args.get("command"))
            if read is not None:
                cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
                for raw_path in read.paths:
                    resolved = resolve_path(raw_path, root, cwd)
                    if (
                        is_file is not None
                        and resolved is not None
                        and resolved.inside
                        and not is_file(resolved.path)
                    ):
                        continue
                    add(
                        "read",
                        raw_path,
                        "tool_event",
                        "reported",
                        tool=tool,
                        correlation=correlation,
                        cwd=cwd,
                        details={"shell_read": read.as_details()},
                    )
            return out
        if raw is None:
            return out
        if op == "read":
            add("read", raw, "tool_event", "reported", tool=tool, correlation=correlation)
        elif op == "modify":
            add("modify", raw, "tool_event", "reported", tool=tool, correlation=correlation)
        else:
            add(
                "search" if op == "search" else "reference",
                raw,
                "tool_event",
                "inferred",
                tool=tool,
                correlation=correlation,
            )
        return out
    if etype == "command":
        argv = payload.get("argv") if isinstance(payload.get("argv"), list) else []
        if not argv or is_file is None:
            return out
        cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
        display = " ".join(str(a) for a in argv)[:500]
        seen: set[str] = set()
        for arg in argv[1:64]:
            if not isinstance(arg, str) or arg.startswith("-") or "\n" in arg or len(arg) > 400:
                continue
            resolved = resolve_path(arg, root, cwd)
            # Only arguments naming an existing file inside the boundary count: a command
            # may reference anything, and a reference is not a read.
            if resolved is None or not resolved.inside or resolved.path in seen:
                continue
            if not is_file(resolved.path):
                continue
            seen.add(resolved.path)
            add(
                "reference",
                resolved.path,
                "command_reference",
                "inferred",
                command=display,
                correlation=str(payload.get("pid") or "") or None,
                details={"argv0": PurePosixPath(str(argv[0])).name},
            )
        return out
    return out


# --------------------------------------------------------------------------- summaries


@dataclass
class Snapshot:
    """A start or end snapshot as the ledger sees it."""

    is_repo: bool
    files: dict[str, list[Any]]
    head: str | None = None
    taken_at: datetime | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def state(self, path: str) -> FileState | None:
        return FileState.from_list(self.files.get(path))

    def knows(self, path: str) -> bool:
        return path in self.files

    def absent_known(self, path: str) -> bool:
        """True when the snapshot proves ``path`` did not exist (not merely unrecorded).

        A start snapshot lists every tracked and untracked file; Git-ignored paths are
        only listed as ignored entries (``details.ignored``, directories end in ``/``),
        so a path under one of them has an unknown starting state.
        """
        if self.details.get("truncated"):
            return False
        ignored = self.details.get("ignored") or []
        if self.details.get("ignored_truncated"):
            return False
        for entry in ignored:
            entry = str(entry)
            if path == entry.rstrip("/") or (entry.endswith("/") and path.startswith(entry)):
                return False
        return True


@dataclass(frozen=True)
class CommandWindow:
    display: str
    category: str | None
    started: datetime
    finished: datetime


@dataclass
class Summary:
    file_key: str
    start_path: str | None
    end_path: str | None
    rename_chain: list[str]
    first_at: datetime | None
    last_at: datetime | None
    operation_counts: dict[str, int]
    operation_total: int
    first_step: str | None
    last_step: str | None
    attempts: list[str]
    agents: list[str]
    tools: list[str]
    sources: list[str]
    classification: str
    classifications: list[str]
    start_hash: str | None
    end_hash: str | None
    size_bytes: int | None
    is_binary: bool | None
    net_status: str
    insertions: int | None
    deletions: int | None
    net_unchanged: bool
    is_protected: bool
    protected_patterns: list[str]
    is_generated: bool
    is_outside: bool
    is_collapsed: bool
    collapsed_count: int | None
    completeness: str
    explanation: str
    policy: dict[str, Any] | None


@dataclass(frozen=True)
class LedgerEvent:
    """An evidence row as the ledger reads it back (ORM row or draft)."""

    sequence: int
    ts: datetime
    operation: str
    path: str
    previous_path: str | None
    inside_boundary: bool
    source: str
    count: int
    attempt_id: str | None
    node_key: str | None
    adapter: str | None
    tool: str | None
    command: str | None
    after_hash: str | None
    policy: dict[str, Any] | None
    details: Mapping[str, Any]


def _union_find(events: Sequence[LedgerEvent]) -> dict[str, str]:
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for e in events:
        find(e.path)
        if e.operation == "rename" and e.previous_path:
            a, b = find(e.previous_path), find(e.path)
            if a != b:
                parent[max(a, b)] = min(a, b)
    return {p: find(p) for p in list(parent)}


def _in_window(ts: datetime, windows: Sequence[CommandWindow]) -> CommandWindow | None:
    from datetime import timedelta

    slack = timedelta(seconds=COMMAND_WINDOW_SLACK_SECONDS)
    # A command running at ``ts`` wins over one that finished within the slack.
    for extra in (timedelta(0), slack):
        covering = [w for w in windows if w.started <= ts <= w.finished + extra]
        if covering:
            return max(covering, key=lambda w: w.started)
    return None


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def build_summaries(
    events: Sequence[LedgerEvent],
    start: Snapshot | None,
    end: Snapshot | None,
    *,
    patterns: Sequence[str] = (),
    commands: Sequence[CommandWindow] = (),
    watcher_active: bool = True,
) -> list[Summary]:
    """One summary per logical file, deterministically ordered by ``file_key``."""
    ordered = sorted(events, key=lambda e: (e.ts, e.sequence))
    collapsed = [e for e in ordered if e.operation == "collapsed"]
    individual = [e for e in ordered if e.operation != "collapsed"]
    roots = _union_find(individual)
    groups: dict[str, list[LedgerEvent]] = {}
    for e in individual:
        groups.setdefault(roots[e.path], []).append(e)
    numstat = dict((end.details.get("numstat") if end else None) or {})
    out: list[Summary] = []
    for evs in groups.values():
        out.append(
            _summarize(
                evs,
                start,
                end,
                patterns=patterns,
                commands=commands,
                watcher_active=watcher_active,
                numstat=numstat,
            )
        )
    out.extend(_collapsed_summaries(collapsed))
    return sorted(out, key=lambda s: (s.is_collapsed, s.file_key))


def _summarize(
    evs: list[LedgerEvent],
    start: Snapshot | None,
    end: Snapshot | None,
    *,
    patterns: Sequence[str],
    commands: Sequence[CommandWindow],
    watcher_active: bool,
    numstat: Mapping[str, Any],
) -> Summary:
    renames = [e for e in evs if e.operation == "rename" and e.previous_path]
    chain: list[str] = []
    if renames:
        chain = [str(renames[0].previous_path)]
        for r in renames:
            if chain[-2:] == [r.previous_path, r.path]:
                continue  # the same rename seen by another source (agent report, watcher)
            if chain[-1] != r.previous_path:
                chain.append(str(r.previous_path))
            chain.append(r.path)
        start_path, end_path = chain[0], chain[-1]
    else:
        start_path = end_path = evs[0].path
    all_paths = sorted({e.path for e in evs} | {str(e.previous_path) for e in renames})

    # One physical write is often reported by the agent, seen by the watcher, and found
    # again by Git: count each operation once, from the source that saw it most often.
    by_source: Counter[str] = Counter()
    by_op_source: Counter[tuple[str, str]] = Counter()
    for e in evs:
        by_source[e.source] += e.count
        by_op_source[(e.operation, e.source)] += e.count
    counts: Counter[str] = Counter()
    for (op, _src), n in by_op_source.items():
        counts[op] = max(counts[op], n)
    sources = sorted(by_source)
    steps = [e.node_key for e in evs if e.node_key]
    attempts = sorted({e.attempt_id for e in evs if e.attempt_id})
    agents = sorted({e.adapter for e in evs if e.adapter and e.source != "filesystem_watcher"})
    tools = sorted(
        {e.tool for e in evs if e.tool}
        | {f"command: {e.command}" for e in evs if e.command and e.source == "command_reference"}
    )
    outside = any(not e.inside_boundary for e in evs)
    protected = sorted({m for p in all_paths for m in matches_protected(p, patterns)})
    policy = next((e.policy for e in reversed(evs) if e.policy), None)

    write_events = [e for e in evs if e.operation in WRITE_OPS]
    watcher_writes = [e for e in write_events if e.source == "filesystem_watcher"]
    reported_writes = [e for e in write_events if e.source in ("agent_hook", "tool_event")]
    reads = counts["read"]
    refs = counts["search"] + counts["reference"]

    # Commands whose window covers watcher-only writes (generated files, attribution).
    command_hits: list[CommandWindow] = []
    for e in watcher_writes:
        w = _in_window(e.ts, commands)
        if w is not None and w not in command_hits:
            command_hits.append(w)
    for w in command_hits:
        tools = sorted(set(tools) | {f"command: {w.display}"})

    s_state = start.state(start_path) if start is not None else None
    start_known = start is not None and (start.knows(start_path) or start.absent_known(start_path))
    e_state: FileState | None = None
    if end is not None:
        if end.knows(end_path) or not end.is_repo:
            e_state = end.state(end_path)
        elif start is not None:
            # A Git end snapshot lists every path Git reports as changed plus every path
            # with evidence; anything else is unchanged since the start.
            e_state = start.state(end_path)

    if outside:
        net = "unknown"
    elif end is None:
        net = "pending"
    elif not start_known:
        net = "unknown"
    elif s_state is None and e_state is None:
        net = "unchanged"
    elif s_state is None:
        net = "added"
    elif e_state is None:
        net = "deleted"
    elif start_path != end_path:
        net = "renamed"
    elif s_state.hash is None or e_state.hash is None:
        # Too large to hash: only a size difference proves a change.
        net = "modified" if s_state.size != e_state.size else "unknown"
    elif s_state.hash == e_state.hash:
        net = "unchanged"
    else:
        net = "modified"

    has_writes = bool(write_events)
    intermediate_change = bool(
        s_state is not None
        and s_state.hash is not None
        and any(
            (e.after_hash and e.after_hash != s_state.hash)
            or any(h != s_state.hash for h in e.details.get("observed_hashes") or [])
            for e in write_events
        )
    )
    generated = bool(
        command_hits
        and not reported_writes
        and any(w.category in GENERATING_CATEGORIES for w in command_hits)
        and net in ("added", "modified", "pending")
    )

    labels: list[str] = []
    if reads:
        labels.append("observed_read")
    if refs:
        labels.append("inferred_access")
    for op, label in (
        ("create", "created"),
        ("modify", "modified"),
        ("delete", "deleted"),
        ("rename", "renamed"),
    ):
        if counts[op]:
            labels.append(label)

    completeness = "observed"
    if outside:
        final = "outside_boundary"
    elif net == "pending" or (net == "unknown" and end is not None and not start_known):
        final = _provisional(counts)
        if generated and final in ("created", "modified"):
            final = "generated"
    elif net == "renamed":
        final = "renamed"
    elif net == "added":
        final = "generated" if generated else "created"
    elif net == "deleted":
        final = "deleted"
    elif net == "modified":
        final = "generated" if generated else "modified"
    elif net == "unchanged":
        if has_writes:
            if s_state is None:
                final = "net_unchanged"  # created and removed again
            elif (watcher_writes and intermediate_change) or (
                reported_writes and (watcher_writes or not watcher_active)
            ):
                final = "restored"
            elif reported_writes:
                final = "unknown"  # reported, but neither the watcher nor Git saw a change
            else:
                final = "net_unchanged"
        elif reads:
            final = "observed_read"
        elif refs:
            final = "inferred_access"
        else:
            final = "unknown"
    else:
        final = _provisional(counts) if has_writes else "unknown"
    if final not in labels:
        labels.append(final)
    net_unchanged = net == "unchanged" and has_writes

    if net == "pending":
        completeness = "pending"
    elif not write_events and not reads and refs:
        completeness = "inferred"
    elif set(sources) <= {"git_end", "reconciliation"}:
        completeness = "reconciled_only"
    elif (
        not watcher_writes and reported_writes and not {"git_end", "reconciliation"} & set(sources)
    ):
        completeness = "telemetry_only" if net in ("unchanged", "unknown") else "observed"

    stat = numstat.get(end_path) or numstat.get(start_path) or []
    insertions = stat[0] if len(stat) > 0 and net not in ("unchanged", "pending") else None
    deletions = stat[1] if len(stat) > 1 and net not in ("unchanged", "pending") else None
    if net == "unchanged":
        insertions = deletions = 0 if start_known and end is not None else None

    explanation = _explain(
        counts=counts,
        by_source=by_source,
        final=final,
        net=net,
        start_path=start_path,
        end_path=end_path,
        chain=chain,
        s_state=s_state,
        e_state=e_state,
        outside=outside,
        protected=protected,
        command_hits=command_hits,
        start_known=start_known,
        watcher_active=watcher_active,
    )
    size = e_state.size if e_state else (s_state.size if s_state else None)
    binary = e_state.binary if e_state else (s_state.binary if s_state else None)
    return Summary(
        file_key=end_path,
        start_path=start_path if (s_state is not None or not start_known) else None,
        end_path=end_path if (e_state is not None or end is None) else None,
        rename_chain=chain,
        first_at=evs[0].ts,
        last_at=max(e.ts for e in evs),
        operation_counts={k: counts[k] for k in sorted(counts)},
        operation_total=sum(counts.values()),
        first_step=steps[0] if steps else None,
        last_step=steps[-1] if steps else None,
        attempts=attempts,
        agents=agents,
        tools=tools[:20],
        sources=sources,
        classification=final,
        classifications=labels,
        start_hash=s_state.hash if s_state else None,
        end_hash=e_state.hash if e_state else None,
        size_bytes=size,
        is_binary=binary,
        net_status=net,
        insertions=insertions,
        deletions=deletions,
        net_unchanged=net_unchanged,
        is_protected=bool(protected),
        protected_patterns=protected,
        is_generated=final == "generated",
        is_outside=outside,
        is_collapsed=False,
        collapsed_count=None,
        completeness=completeness,
        explanation=explanation,
        policy=policy,
    )


def _provisional(counts: Counter[str]) -> str:
    for op, label in (
        ("rename", "renamed"),
        ("delete", "deleted"),
        ("create", "created"),
        ("modify", "modified"),
        ("read", "observed_read"),
        ("search", "inferred_access"),
        ("reference", "inferred_access"),
    ):
        if counts[op]:
            return label
    return "unknown"


_SOURCE_PHRASES = {
    "agent_hook": "the agent reported it",
    "tool_event": "a tool call named it",
    "filesystem_watcher": "the filesystem watcher saw it change",
    "command_reference": "a command's arguments named it",
    "git_end": "the final Git state differs from the start",
    "reconciliation": "Git reconciliation found a change the watcher did not report",
    "git_start": "the starting Git snapshot recorded it",
    "manual": "it was recorded manually",
}


def _explain(
    *,
    counts: Counter[str],
    by_source: Counter[str],
    final: str,
    net: str,
    start_path: str,
    end_path: str,
    chain: list[str],
    s_state: FileState | None,
    e_state: FileState | None,
    outside: bool,
    protected: list[str],
    command_hits: list[CommandWindow],
    start_known: bool,
    watcher_active: bool,
) -> str:
    parts: list[str] = []
    ops = ", ".join(_plural(counts[k], k) for k in OPERATIONS if counts[k])
    srcs = "; ".join(
        f"{_SOURCE_PHRASES.get(s, s)} ({n}x)" for s, n in sorted(by_source.items()) if n
    )
    parts.append(f"Evidence: {ops}. Why it appears: {srcs}.")
    if counts["read"]:
        parts.append(
            "The read was reported by agent or tool telemetry; the filesystem watcher cannot "
            "observe reads."
        )
    if counts["search"] or counts["reference"]:
        parts.append("A search result or command argument named it; a true read is not proven.")
    if chain:
        parts.append("Renamed: " + " -> ".join(chain) + ".")
    for w in command_hits:
        parts.append(f"Watcher activity happened while `{w.display}` was running.")
    if outside:
        parts.append(
            "The path is outside the run's permitted project root; AgentTape did not read it."
        )
    if net == "pending":
        parts.append("Final Git reconciliation has not run yet (the attempt is still active).")
    elif not start_known:
        parts.append(
            "Its starting state was not captured (no start snapshot, or ignored by Git at "
            "start), so the net change is unknown."
        )
    elif net == "unchanged" and final == "restored":
        parts.append(
            f"It changed during the run and ended with its original content hash "
            f"({((s_state.hash if s_state else None) or '')[:10]}), so it is restored with no net change."
        )
    elif net == "unchanged" and final == "net_unchanged" and s_state is None:
        parts.append("It did not exist at the start and does not exist at the end.")
    elif net == "unchanged" and final == "net_unchanged":
        parts.append(
            "Activity was observed but its final content matches the start; the watcher "
            "does not prove the content ever differed."
        )
    elif net == "unchanged" and final == "unknown":
        parts.append(
            "The agent reported a write, but neither the filesystem watcher nor Git saw a "
            "change in the run directory."
        )
    elif net in ("added", "modified", "deleted", "renamed"):
        parts.append(f"Net Git status at the end: {net}.")
    if not watcher_active and net != "pending":
        parts.append("Filesystem observation was unavailable for this run.")
    if protected:
        parts.append("Matches protected-path rule(s): " + ", ".join(protected) + ".")
    return " ".join(parts)


def _collapsed_summaries(events: Sequence[LedgerEvent]) -> list[Summary]:
    buckets: dict[str, list[LedgerEvent]] = {}
    for e in events:
        buckets.setdefault(e.path, []).append(e)
    out: list[Summary] = []
    for bucket, evs in buckets.items():
        counts: Counter[str] = Counter()
        for e in evs:
            for op, n in dict(e.details.get("by_operation") or {}).items():
                counts[str(op)] += int(n)
        total = sum(e.count for e in evs)
        ops = ", ".join(f"{n} {op}" for op, n in sorted(counts.items()))
        out.append(
            Summary(
                file_key=bucket,
                start_path=None,
                end_path=None,
                rename_chain=[],
                first_at=evs[0].ts,
                last_at=max(e.ts for e in evs),
                operation_counts={k: counts[k] for k in sorted(counts)},
                operation_total=total,
                first_step=next((e.node_key for e in evs if e.node_key), None),
                last_step=next((e.node_key for e in reversed(evs) if e.node_key), None),
                attempts=sorted({e.attempt_id for e in evs if e.attempt_id}),
                agents=[],
                tools=[],
                sources=["filesystem_watcher"],
                classification="unknown",
                classifications=["collapsed"],
                start_hash=None,
                end_hash=None,
                size_bytes=None,
                is_binary=None,
                net_status="unknown",
                insertions=None,
                deletions=None,
                net_unchanged=False,
                is_protected=False,
                protected_patterns=[],
                is_generated=False,
                is_outside=False,
                is_collapsed=True,
                collapsed_count=total,
                completeness="observed",
                explanation=(
                    f"{total:,} filesystem events under {bucket}, collapsed by the noise policy "
                    f"({ops}). Individual files were not hashed or read."
                ),
                policy=None,
            )
        )
    return out


# --------------------------------------------------------------------------- completeness

#: Adapters whose telemetry explicitly reports file reads (docs/FILE-EVIDENCE.md).
READ_REPORTING_ADAPTERS = frozenset({"demo", "claude-code"})


@dataclass(frozen=True)
class ObserverWindow:
    attempt_id: str
    status: str  #: active | unavailable | degraded
    reason: str | None
    started_at: datetime | None
    stopped_at: datetime | None
    attempt_finished: bool
    dropped: int = 0


@dataclass(frozen=True)
class OfflineWindow:
    offline_from: str | None
    offline_to: str | None


@dataclass(frozen=True)
class ObservedReads:
    """How an observed or Herdr-hosted Claude Code session's reads reached AgentTape:
    only through its hooks. ``file_evidence.store.completeness`` builds this from
    ``agenttape.findings.verify.observed_gaps``/``observed_statement``; this module only
    words it, never decides a gap itself."""

    #: Whether AgentTape received any Claude Code hook from the session at all.
    hooks: bool
    #: ``observed_statement()``: one sentence per coverage gap, ``""`` when the hooks
    #: covered the whole session.
    gap_statement: str


def run_completeness(
    *,
    adapter: str | None,
    observers: Sequence[ObserverWindow],
    has_start: bool,
    has_end: bool,
    is_repo: bool,
    read_events: int,
    run_active: bool,
    offline: Sequence[OfflineWindow] = (),
    observed_reads: ObservedReads | None = None,
) -> dict[str, Any]:
    """The run's evidence completeness statement plus its structured parts.

    ``observed_reads`` is ``None`` for a run AgentTape launched (its reads come from the
    agent telemetry it captured); for a run read only through Claude Code hooks (observed
    or Herdr-hosted) the statement names the session's
    hooks as the only source of reads, and whether they covered the whole session.
    """
    if not observers or all(o.status == "unavailable" for o in observers):
        writes = "unavailable"
    elif any(o.status != "active" for o in observers) or any(
        o.attempt_finished and o.stopped_at is None for o in observers
    ):
        writes = "partial"
    else:
        writes = "observed"
    if offline:
        writes = "partial" if writes == "observed" else writes
    # A run read only through Claude Code hooks that did reach it has read telemetry from
    # those hooks (they report every tool call), even when none of the calls was a read
    # and its own adapter (Herdr) reports none.
    hook_reads = observed_reads is not None and observed_reads.hooks
    reads = (
        "telemetry"
        if read_events or hook_reads or adapter in READ_REPORTING_ADAPTERS
        else "unavailable"
    )
    if has_end:
        git = "completed"
    elif run_active:
        git = "pending"
    elif has_start:
        git = "missing"
    else:
        git = "unavailable"
    if has_end and not is_repo:
        git = "filesystem_only"

    sentences: list[str] = []
    if offline:
        spans = "; ".join(
            f"from {o.offline_from or 'an unknown time'} to {o.offline_to}" for o in offline
        )
        sentences.append(
            f"Partial: AgentTape was offline {spans}; final Git reconciliation "
            + ("still ran." if has_end else "has not run yet.")
        )
    if writes == "observed" and git == "completed":
        sentences.append("Writes and final changes fully observed")
    elif writes == "observed" and git == "pending":
        sentences.append(
            "Writes observed live; final Git reconciliation runs when the attempt ends"
        )
    elif writes == "partial":
        sentences.append(
            "Filesystem observation was partial (degraded or interrupted); "
            + (
                "final Git reconciliation completed"
                if git == "completed"
                else "final Git reconciliation pending"
                if git == "pending"
                else "final Git reconciliation unavailable"
            )
        )
    elif writes == "unavailable":
        sentences.append(
            "Filesystem observation unavailable; "
            + (
                "final Git reconciliation completed"
                if git == "completed"
                else "final Git reconciliation pending"
                if git == "pending"
                else "final state comparison completed without Git"
                if git == "filesystem_only"
                else "final Git reconciliation unavailable"
            )
        )
    else:
        sentences.append(
            "Writes observed; "
            + (
                "final state compared without Git (not a Git repository)"
                if git == "filesystem_only"
                else "final Git reconciliation did not run"
            )
        )
    if observed_reads is not None and not observed_reads.hooks:
        sentences[-1] += "."
        sentences.append(observed_reads.gap_statement)
    elif observed_reads is not None and reads == "telemetry":
        if observed_reads.gap_statement:
            sentences[-1] += (
                "; reads available only from Claude Code hooks, which did not cover the "
                "whole session."
            )
            sentences.append(observed_reads.gap_statement)
        else:
            sentences[-1] += (
                "; reads available only from Claude Code hooks, which AgentTape received for "
                "the whole session."
            )
    elif reads == "telemetry":
        sentences[-1] += "; reads available only from agent telemetry."
    else:
        sentences[-1] += "."
        sentences.append("Agent telemetry unavailable; read activity not captured.")
    level = (
        "pending"
        if git == "pending"
        else "complete"
        if writes == "observed" and git == "completed" and not offline
        else "partial"
        if git in ("completed", "filesystem_only") or writes != "unavailable"
        else "unavailable"
    )
    return {
        "level": level,
        "statement": " ".join(sentences),
        "writes": writes,
        "reads": reads,
        "git": git,
        "offline_windows": [
            {"offline_from": o.offline_from, "offline_to": o.offline_to} for o in offline
        ],
        "dropped_watcher_events": sum(o.dropped for o in observers),
    }


def rename_pairs(
    deleted: Iterable[str],
    added: Iterable[str],
    last_hash: Callable[[str], str | None],
    new_hash: Callable[[str], str | None],
) -> list[tuple[str, str]]:
    """Exact renames: a deleted path whose last known hash equals an added path's hash."""
    by_hash: dict[str, list[str]] = {}
    for d in sorted(deleted):
        h = last_hash(d)
        if h:
            by_hash.setdefault(h, []).append(d)
    pairs: list[tuple[str, str]] = []
    for a in sorted(added):
        h = new_hash(a)
        if h and by_hash.get(h):
            pairs.append((by_hash[h].pop(0), a))
    return pairs
