"""File Evidence rules: classifications, coalescing, renames, reconciliation, noise,
protected paths and boundary, completeness, telemetry mapping, determinism, bounds."""

from __future__ import annotations

import random
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agenttape.adapters.builtin.demo import DemoAdapter
from agenttape.file_evidence.collector import MAX_PENDING_PATHS, Collector
from agenttape.file_evidence.ledger import (
    ABSENT,
    CommandWindow,
    EventRow,
    FileState,
    LedgerEvent,
    NoisePolicy,
    ObservedReads,
    ObserverWindow,
    OfflineWindow,
    Snapshot,
    blob_hash,
    build_summaries,
    hash_file,
    is_temp_save,
    resolve_path,
    run_completeness,
    telemetry_evidence,
)
from agenttape.security.redaction import Redactor

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
H_A, H_B, H_C = "a" * 40, "b" * 40, "c" * 40


def ev(
    op: str,
    path: str,
    source: str = "filesystem_watcher",
    *,
    t: int = 0,
    prev: str | None = None,
    after: str | None = None,
    count: int = 1,
    inside: bool = True,
    details: dict[str, Any] | None = None,
    seq: int | None = None,
) -> LedgerEvent:
    return LedgerEvent(
        sequence=seq if seq is not None else t,
        ts=T0 + timedelta(seconds=t),
        operation=op,
        path=path,
        previous_path=prev,
        inside_boundary=inside,
        source=source,
        count=count,
        attempt_id="att-1",
        node_key=None,
        adapter="demo" if source in ("agent_hook", "tool_event") else None,
        tool=None,
        command=None,
        after_hash=after,
        policy=None,
        details=details or {},
    )


def snap(files: dict[str, list[Any]], **details: Any) -> Snapshot:
    return Snapshot(is_repo=True, files=files, details=details)


def one(events: list[LedgerEvent], start: Snapshot | None, end: Snapshot | None, **kw: Any) -> Any:
    rows = [s for s in build_summaries(events, start, end, **kw) if not s.is_collapsed]
    assert len(rows) == 1, rows
    return rows[0]


# --------------------------------------------------------------------------- classifications


def test_created_modified_deleted_classifications() -> None:
    start = snap({"a.py": [H_A, 10, False], "gone.py": [H_B, 5, False]})
    end = snap({"a.py": [H_C, 12, False], "gone.py": ABSENT, "new.py": [H_A, 3, False]})
    rows = {
        s.file_key: s
        for s in build_summaries(
            [
                ev("modify", "a.py", after=H_C),
                ev("delete", "gone.py", t=1),
                ev("create", "new.py", t=2),
            ],
            start,
            end,
        )
    }
    assert rows["a.py"].classification == "modified" and rows["a.py"].net_status == "modified"
    assert rows["gone.py"].classification == "deleted" and rows["gone.py"].end_path is None
    assert rows["new.py"].classification == "created" and rows["new.py"].net_status == "added"
    assert rows["new.py"].start_hash is None and rows["new.py"].end_hash == H_A


def test_restored_file_is_touched_but_net_unchanged() -> None:
    start = snap({"README.md": [H_A, 10, False]})
    end = snap({"README.md": [H_A, 10, False]})
    s = one(
        [ev("modify", "README.md", after=H_B, t=1), ev("modify", "README.md", after=H_A, t=2)],
        start,
        end,
    )
    assert s.classification == "restored"
    assert s.net_status == "unchanged" and s.net_unchanged is True
    assert "restored" in s.explanation and s.insertions == 0 and s.deletions == 0


def test_restore_proven_by_intermediate_hash_seen_within_one_batch() -> None:
    start = snap({"x.txt": [H_A, 1, False]})
    s = one(
        [ev("modify", "x.txt", after=H_A, details={"observed_hashes": [H_B, H_A]})],
        start,
        snap({"x.txt": [H_A, 1, False]}),
    )
    assert s.classification == "restored"


def test_watcher_touch_without_content_change_is_net_unchanged_not_restored() -> None:
    start = snap({"src/__init__.py": [H_A, 0, False]})
    s = one(
        [ev("modify", "src/__init__.py", after=H_A)],
        start,
        snap({"src/__init__.py": [H_A, 0, False]}),
    )
    assert s.classification == "net_unchanged" and s.net_unchanged


def test_created_then_deleted_is_net_unchanged() -> None:
    s = one(
        [ev("create", "tmp.txt", after=H_A), ev("delete", "tmp.txt", t=1)],
        snap({}),
        snap({"tmp.txt": ABSENT}),
    )
    assert s.classification == "net_unchanged" and s.net_unchanged and s.start_path is None


def test_deleted_and_recreated_with_new_content_is_modified() -> None:
    start = snap({"cfg.json": [H_A, 5, False]})
    s = one(
        [ev("delete", "cfg.json"), ev("create", "cfg.json", t=1, after=H_B)],
        start,
        snap({"cfg.json": [H_B, 6, False]}),
    )
    assert s.classification == "modified"
    assert s.operation_counts == {"create": 1, "delete": 1}


def test_reported_write_the_watcher_and_git_never_saw_is_unknown_not_restored() -> None:
    start = snap({"a.py": [H_A, 1, False]})
    s = one([ev("modify", "a.py", "agent_hook")], start, snap({"a.py": [H_A, 1, False]}))
    assert s.classification == "unknown"
    assert "neither the filesystem watcher nor Git" in s.explanation


def test_reported_write_without_a_watcher_ending_unchanged_is_restored() -> None:
    start = snap({"a.py": [H_A, 1, False]})
    s = one(
        [ev("modify", "a.py", "agent_hook")],
        start,
        snap({"a.py": [H_A, 1, False]}),
        watcher_active=False,
    )
    assert s.classification == "restored"
    assert "unavailable" in s.explanation


def test_reads_are_labeled_by_source_and_never_from_the_watcher() -> None:
    start = snap({"docs/a.md": [H_A, 1, False], "docs/b.md": [H_B, 1, False]})
    end = snap({})
    rows = {
        s.file_key: s
        for s in build_summaries(
            [
                ev("read", "docs/a.md", "agent_hook"),
                ev("reference", "docs/b.md", "command_reference"),
            ],
            start,
            end,
        )
    }
    assert rows["docs/a.md"].classification == "observed_read"
    assert "cannot observe reads" in rows["docs/a.md"].explanation
    assert rows["docs/b.md"].classification == "inferred_access"
    assert rows["docs/b.md"].completeness == "inferred"


def test_pending_state_before_reconciliation() -> None:
    s = one([ev("modify", "a.py", after=H_B)], snap({"a.py": [H_A, 1, False]}), None)
    assert s.net_status == "pending" and s.classification == "modified"
    assert s.completeness == "pending" and s.insertions is None


def test_ignored_at_start_has_unknown_starting_state() -> None:
    start = snap({}, ignored=["build/"])
    s = one(
        [ev("create", "build/out.txt", after=H_A)], start, snap({"build/out.txt": [H_A, 1, False]})
    )
    assert s.net_status == "unknown"
    assert "starting state was not captured" in s.explanation


def test_generated_by_an_identified_command() -> None:
    window = CommandWindow("python -m demo_tests", "test", T0, T0 + timedelta(seconds=1))
    s = one(
        [ev("create", "reports/junit.xml", t=2, after=H_A)],
        snap({}),
        snap({"reports/junit.xml": [H_A, 10, False]}),
        commands=[window],
    )
    assert s.classification == "generated" and s.is_generated
    assert "command: python -m demo_tests" in s.tools


# --------------------------------------------------------------------------- renames / coalescing


def test_rename_chain_reconstruction_across_two_renames() -> None:
    start = snap({"a.py": [H_A, 1, False]})
    end = snap({"a.py": ABSENT, "b.py": ABSENT, "c.py": [H_A, 1, False]})
    s = one(
        [
            ev("rename", "b.py", "agent_hook", prev="a.py", t=1),
            ev("rename", "c.py", "reconciliation", prev="b.py", t=2),
        ],
        start,
        end,
    )
    assert s.file_key == "c.py" and s.rename_chain == ["a.py", "b.py", "c.py"]
    assert s.start_path == "a.py" and s.end_path == "c.py"
    assert s.classification == "renamed" and s.net_status == "renamed"


def _collector(
    root: Path, redact: Callable[[str], str] = lambda t: t, **settings: Any
) -> Collector:
    ec = SimpleNamespace(
        workdir=root,
        run=SimpleNamespace(
            id="run-1",
            workspace_id="ws",
            is_demo=False,
            snapshot={"project": {"settings": {"protected_paths": [".github/**"], **settings}}},
        ),
        attempt=SimpleNamespace(id="att-1"),
        adapter_type="demo",
        redact=redact,
    )
    ctx = SimpleNamespace(clock=SimpleNamespace(now=lambda: T0))
    return Collector(cast(Any, ctx), cast(Any, ec), cast(Any, None))


def test_duplicate_watcher_events_coalesce_with_first_last_and_counts(tmp_path: Path) -> None:
    c = _collector(tmp_path)
    for i in range(25):
        c.observe("modify", "src/app.py", T0 + timedelta(milliseconds=i))
    state = FileState(H_B, 3, False)
    drafts = c._watch_drafts(c.pending, {"src/app.py": state})
    c.last_hash["src/app.py"] = H_A
    assert len(drafts) == 1
    d = drafts[0]
    assert d.operation == "modify" and d.count == 25
    assert d.ts == T0 and d.last_ts == T0 + timedelta(milliseconds=24)
    assert d.after_hash == H_B


def test_watcher_rename_pairs_by_content_hash(tmp_path: Path) -> None:
    c = _collector(tmp_path)
    c.last_hash = {"old.py": H_A}
    c.observe("delete", "old.py", T0)
    c.observe("create", "new.py", T0)
    drafts = c._watch_drafts(c.pending, {"old.py": None, "new.py": FileState(H_A, 1, False)})
    assert [(d.operation, d.path, d.previous_path) for d in drafts] == [
        ("rename", "new.py", "old.py")
    ]


def test_created_then_renamed_within_one_flush_is_a_rename(tmp_path: Path) -> None:
    c = _collector(tmp_path)
    c.observe("create", "draft.py", T0)
    c.record_states({"draft.py": FileState(H_A, 1, False)})  # an earlier watcher batch
    c.observe("delete", "draft.py", T0 + timedelta(milliseconds=500))
    c.observe("create", "final.py", T0 + timedelta(milliseconds=500))
    drafts = c._watch_drafts(c.pending, {"draft.py": None, "final.py": FileState(H_A, 1, False)})
    assert [(d.operation, d.path, d.previous_path) for d in drafts] == [
        ("create", "draft.py", None),
        ("rename", "final.py", "draft.py"),
    ]
    assert "draft.py" not in c.last_hash and c.last_hash["final.py"] == H_A


def test_one_physical_write_counts_once_across_sources() -> None:
    start = snap({"a.py": [H_A, 1, False], "x.py": [H_C, 1, False]})
    end = snap({"a.py": [H_B, 1, False], "x.py": ABSENT, "b.py": [H_C, 1, False]})
    modified = one(
        [
            ev("modify", "a.py", "agent_hook", t=1),
            ev("modify", "a.py", t=1),
            ev("modify", "a.py", "git_end", t=3),
        ],
        start,
        end,
    )
    assert modified.operation_counts == {"modify": 1} and modified.operation_total == 1
    renamed = one(
        [
            ev("rename", "b.py", "agent_hook", prev="x.py", t=4),
            ev("rename", "b.py", prev="x.py", t=4),
        ],
        start,
        end,
    )
    assert renamed.operation_counts == {"rename": 1} and renamed.rename_chain == ["x.py", "b.py"]


def test_temporary_save_folds_into_its_target(tmp_path: Path) -> None:
    assert is_temp_save("notes.md.tmp") and is_temp_save(".notes.md.swp")
    c = _collector(tmp_path)
    c.last_hash = {"notes.md": H_A}
    c.observe("create", "notes.md.tmp", T0)
    c.observe("delete", "notes.md.tmp", T0)
    c.observe("create", "notes.md", T0)
    drafts = c._watch_drafts(
        c.pending, {"notes.md.tmp": None, "notes.md": FileState(H_B, 2, False)}
    )
    assert [(d.operation, d.path) for d in drafts] == [("modify", "notes.md")]
    assert drafts[0].details["via_temp"] == ["notes.md.tmp"]


# --------------------------------------------------------------------------- noise


def test_noise_policy_excludes_git_and_collapses_noisy_directories() -> None:
    noise = NoisePolicy.from_settings(
        {"file_evidence": {"collapse": ["logs/"], "capture": ["dist/keep.js"]}}
    )
    assert noise.is_git_internal(".git/index") and noise.is_git_internal(".git")
    assert noise.collapse_bucket("node_modules/a/b.js") == "node_modules/"
    assert noise.collapse_bucket("web/node_modules/x.js") == "web/node_modules/"
    assert noise.collapse_bucket("src/__pycache__/m.cpython-312.pyc") == "src/__pycache__/"
    assert noise.collapse_bucket(".venv/lib/site.py") == ".venv/"
    assert noise.collapse_bucket("coverage/index.html") == "coverage/"
    assert noise.collapse_bucket(".agenttape/state.db") == ".agenttape/"
    assert noise.collapse_bucket("logs/today.log") == "logs/"
    assert noise.collapse_bucket("dist/keep.js") is None  # captured individually by policy
    assert noise.collapse_bucket("src/app.py") is None
    assert noise.collapse_bucket("migrations/0001.sql") is None


def test_collapsed_directories_are_counted_not_listed(tmp_path: Path) -> None:
    c = _collector(tmp_path)
    for i in range(1000):
        assert c.observe("create", f"node_modules/pkg/f{i}.js", T0) is False
    assert c.pending == {}
    drafts = c._collapsed_drafts(c.collapsed, c.collapsed_times)
    assert len(drafts) == 1 and drafts[0].count == 1000 and drafts[0].path == "node_modules/"
    rows = build_summaries(
        [ev("collapsed", "node_modules/", count=1000, details={"by_operation": {"create": 1000}})],
        snap({}),
        snap({}),
    )
    assert rows[0].is_collapsed and rows[0].collapsed_count == 1000
    assert "1,000 filesystem events under node_modules/" in rows[0].explanation


def test_ten_thousand_filesystem_events_stay_fast_and_bounded(tmp_path: Path) -> None:
    c = _collector(tmp_path)
    started = time.monotonic()
    for i in range(10_000):
        if i % 10 == 0:
            c.observe("modify", f"src/file{(i // 10) % 50}.py", T0 + timedelta(microseconds=i))
        else:
            c.observe("create", f"node_modules/p{i % 7}/f{i}.js", T0)
    states: dict[str, FileState | str | None] = {
        rel: FileState(blob_hash(rel.encode()), 1, False) for rel in c.pending
    }
    drafts = c._watch_drafts(c.pending, states) + c._collapsed_drafts(
        c.collapsed, c.collapsed_times
    )
    assert time.monotonic() - started < 2.0
    assert len(c.pending) == 50  # distinct individually tracked paths only
    assert len(drafts) == 51  # 50 coalesced files + one collapsed directory
    assert sum(d.count for d in drafts if d.operation == "collapsed") == 9_000


def _as_ledger(drafts: list[Any]) -> list[LedgerEvent]:
    return [
        ev(d.operation, d.path, t=i, count=d.count, after=d.after_hash, details=d.details)
        for i, d in enumerate(drafts)
    ]


def test_a_burst_of_collapsed_writes_is_one_exactly_counted_bucket(tmp_path: Path) -> None:
    """The story test's 400 node_modules writes, counted exactly (no watcher, no clock)."""
    c = _collector(tmp_path)
    for i in range(400):
        assert c.observe("create", f"node_modules/pkg/{i // 100}/f{i}.js", T0) is False
    assert c.pending == {} and c.dropped == 0
    assert c.collapsed == {"node_modules/": Counter(create=400)}
    drafts = c._collapsed_drafts(c.collapsed, c.collapsed_times)
    assert [(d.operation, d.path, d.count) for d in drafts] == [("collapsed", "node_modules/", 400)]
    assert drafts[0].details["by_operation"] == {"create": 400}
    (row,) = build_summaries(_as_ledger(drafts), snap({}), snap({}))
    assert row.is_collapsed and row.collapsed_count == 400
    assert row.operation_counts == {"create": 400}


def test_ten_thousand_collapsed_events_and_twenty_sources_are_counted_exactly(
    tmp_path: Path,
) -> None:
    """The 10,000-event integration test's magnitudes, deterministic (no watcher, no clock)."""
    c = _collector(tmp_path)
    for i in range(10_000):
        c.observe("create", f"node_modules/p{i // 500}/f{i}.js", T0)
    for i in range(20):
        c.observe("create", f"src/gen{i}.py", T0)
    assert len(c.pending) == 20 and c.dropped == 0
    assert sum(c.collapsed["node_modules/"].values()) == 10_000
    hashes = {rel: blob_hash(rel.encode()) for rel in c.pending}
    states: dict[str, FileState | str | None] = {
        rel: FileState(h, 1, False) for rel, h in hashes.items()
    }
    drafts = c._watch_drafts(c.pending, states) + c._collapsed_drafts(
        c.collapsed, c.collapsed_times
    )
    assert len(drafts) == 21  # 20 created source files + one collapsed node_modules/
    rows = {
        s.file_key: s
        for s in build_summaries(
            _as_ledger(drafts),
            snap({}),
            snap({rel: [h, 1, False] for rel, h in hashes.items()}),
        )
    }
    assert len(rows) == 21
    assert sum(1 for r in rows.values() if r.classification == "created") == 20
    collapsed = rows["node_modules/"]
    assert collapsed.is_collapsed and collapsed.collapsed_count == 10_000
    assert collapsed.operation_counts == {"create": 10_000}


def test_pending_paths_are_bounded_and_overflow_is_counted(tmp_path: Path) -> None:
    c = _collector(tmp_path)
    for i in range(MAX_PENDING_PATHS + 25):
        c.observe("create", f"gen/f{i}.txt", T0)
    assert len(c.pending) == MAX_PENDING_PATHS and c.dropped == 25


# --------------------------------------------------------------------------- policy / boundary


def test_protected_and_outside_boundary_flags() -> None:
    root = Path("/work/repo")
    assert resolve_path("src/a.py", root) is not None
    inside = resolve_path("../repo/src/a.py", root, "src")
    assert inside is not None and inside.inside and inside.path == "repo/src/a.py"
    outside = resolve_path("../../etc/passwd", root)
    assert outside is not None and not outside.inside and outside.path == "/etc/passwd"
    absolute_in = resolve_path("/work/repo/src/b.py", root)
    assert absolute_in is not None and absolute_in.inside and absolute_in.path == "src/b.py"
    rows = {
        s.file_key: s
        for s in build_summaries(
            [
                ev("modify", ".github/workflows/ci.yml", "agent_hook", after=H_B),
                ev("modify", "/etc/hosts", "agent_hook", inside=False, t=1),
            ],
            snap({".github/workflows/ci.yml": [H_A, 1, False]}),
            snap({".github/workflows/ci.yml": [H_B, 2, False]}),
            patterns=[".github/**"],
        )
    }
    assert rows[".github/workflows/ci.yml"].is_protected
    assert rows[".github/workflows/ci.yml"].protected_patterns == [".github/**"]
    assert rows["/etc/hosts"].classification == "outside_boundary" and rows["/etc/hosts"].is_outside
    assert "did not read it" in rows["/etc/hosts"].explanation


def test_path_traversal_never_resolves_inside_through_dotdot() -> None:
    root = Path("/work/repo")
    for raw in ("../secrets.txt", "src/../../other/x", "/work/repo-evil/x", "~/.ssh/id_rsa"):
        resolved = resolve_path(raw, root)
        assert resolved is not None and not resolved.inside, raw


# --------------------------------------------------------------------------- telemetry


def _row(type_: str, payload: dict[str, Any], source: str = "demo") -> EventRow:
    return EventRow(id="e1", sequence=1, ts=T0, type=type_, source=source, payload=payload)


def test_telemetry_mapping_reads_writes_renames_tools_and_commands(tmp_path: Path) -> None:
    root = tmp_path
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("x")
    drafts = telemetry_evidence(_row("file_read", {"path": "src/a.py", "bytes": 1}), root)
    assert [(d.operation, d.source, d.quality) for d in drafts] == [
        ("read", "agent_hook", "reported")
    ]
    drafts = telemetry_evidence(
        _row("file_write", {"path": "b.py", "old_path": "a.py", "operation": "rename"}), root
    )
    assert drafts[0].operation == "rename" and drafts[0].previous_path == "a.py"
    drafts = telemetry_evidence(
        _row("tool_call", {"tool": "Read", "input": {"file_path": str(root / "src/a.py")}}), root
    )
    assert (
        drafts[0].operation == "read"
        and drafts[0].path == "src/a.py"
        and drafts[0].source == "tool_event"
    )
    drafts = telemetry_evidence(_row("tool_call", {"tool": "Grep", "input": {"path": "src"}}), root)
    assert drafts[0].operation == "search" and drafts[0].quality == "inferred"
    drafts = telemetry_evidence(
        _row("command", {"argv": ["pytest", "src/a.py", "-q", "missing.py", "/etc/passwd"]}),
        root,
        is_file=lambda rel: (root / rel).is_file(),
    )
    assert [(d.operation, d.path, d.source) for d in drafts] == [
        ("reference", "src/a.py", "command_reference")
    ]
    assert telemetry_evidence(_row("file_read", {"path": "x"}, source="agenttape"), root) == []
    drafts = telemetry_evidence(_row("file_write", {"path": "/tmp/elsewhere.txt"}), root)
    assert drafts[0].inside_boundary is False


def test_bash_shell_read_creates_a_read_draft_with_shell_read_details() -> None:
    drafts = telemetry_evidence(
        _row("tool_call", {"tool": "Bash", "input": {"command": "sed -n '3,9p' src/a.py"}}),
        Path("/work/repo"),
        is_file=lambda rel: True,
    )
    assert len(drafts) == 1
    d = drafts[0]
    assert d.operation == "read"
    assert d.path == "src/a.py"
    assert d.source == "tool_event"
    assert d.quality == "reported"
    assert d.tool == "Bash"
    assert d.details["shell_read"] == {"program": "sed", "line_start": 3, "line_end": 9}


def test_bash_cat_multiple_paths_creates_distinct_dedupe_keys() -> None:
    drafts = telemetry_evidence(
        _row("tool_call", {"tool": "Bash", "input": {"command": "cat a.py b.py"}}),
        Path("/work/repo"),
        is_file=lambda rel: True,
    )
    assert [d.operation for d in drafts] == ["read", "read"]
    assert {d.path for d in drafts} == {"a.py", "b.py"}
    assert len({d.dedupe_key for d in drafts}) == 2


def test_bash_shell_read_of_a_nonexistent_relative_path_creates_no_draft() -> None:
    drafts = telemetry_evidence(
        _row("tool_call", {"tool": "Bash", "input": {"command": "cat missing.py"}}),
        Path("/work/repo"),
        is_file=lambda rel: False,
    )
    assert drafts == []


def test_bash_shell_read_outside_the_boundary_is_still_a_draft() -> None:
    drafts = telemetry_evidence(
        _row("tool_call", {"tool": "Bash", "input": {"command": "cat /etc/hosts"}}),
        Path("/work/repo"),
        is_file=lambda rel: False,
    )
    assert len(drafts) == 1 and drafts[0].inside_boundary is False


def test_bash_shell_read_relative_path_resolves_against_payload_cwd() -> None:
    drafts = telemetry_evidence(
        _row("tool_call", {"tool": "Bash", "input": {"command": "cat a.py"}, "cwd": "src"}),
        Path("/work/repo"),
        is_file=lambda rel: True,
    )
    assert len(drafts) == 1 and drafts[0].path == "src/a.py"


@pytest.mark.parametrize("command", ["cat a.py | wc -l", "rm a.py"])
def test_bash_non_read_commands_create_no_draft(command: str) -> None:
    drafts = telemetry_evidence(
        _row("tool_call", {"tool": "Bash", "input": {"command": command}}),
        Path("/work/repo"),
        is_file=lambda rel: True,
    )
    assert drafts == []


def test_bash_shell_read_drafts_classify_as_observed_read() -> None:
    drafts = telemetry_evidence(
        _row("tool_call", {"tool": "Bash", "input": {"command": "cat src/a.py"}}),
        Path("/work/repo"),
        is_file=lambda rel: True,
    )
    events = [ev(d.operation, d.path, d.source, details=d.details) for d in drafts]
    s = one(
        events,
        snap({"src/a.py": [H_A, 1, False]}),
        snap({"src/a.py": [H_A, 1, False]}),
    )
    assert s.classification == "observed_read"


def test_demo_adapter_normalizes_every_evidence_record_type() -> None:
    a = DemoAdapter()
    records: list[dict[str, Any]] = [
        {"type": "file_search", "query": "greet", "paths": ["a.py"]},
        {"type": "file_rename", "path": "b.py", "old_path": "a.py"},
        {"type": "command", "argv": ["python", "-m", "x"], "cwd": ".", "exit_code": 0},
        {"type": "context", "stage": "retrieved", "source": {"id": "s", "name": "S"}},
        {"type": "finding", "id": "F1", "kind": "finding", "text": "t", "links": []},
        {"type": "correction", "text": "no", "category": None},
        {"type": "approval_request", "title": "t", "rationale": "r", "paths": ["x"]},
    ]
    types = [a.normalize(r)[0].type for r in records]
    assert [str(t) for t in types] == [
        "file_read",
        "file_write",
        "command",
        "context",
        "finding",
        "context",
        "agent_message",
    ]
    assert a.normalize({"type": "correction", "text": "no"})[0].payload["stage"] == "correction"
    assert a.normalize({"type": "exit", "code": 0}) == []


# --------------------------------------------------------------------------- completeness


def _obs(status: str = "active", stopped: bool = True) -> ObserverWindow:
    return ObserverWindow("att", status, None, T0, T0 if stopped else None, True)


def test_completeness_statements() -> None:
    full = run_completeness(
        adapter="demo", observers=[_obs()], has_start=True, has_end=True, is_repo=True,
        read_events=2, run_active=False,
    )  # fmt: skip
    assert full["level"] == "complete"
    assert full["statement"] == (
        "Writes and final changes fully observed; reads available only from agent telemetry."
    )
    no_watch = run_completeness(
        adapter="generic_command", observers=[_obs("unavailable")], has_start=True,
        has_end=True, is_repo=True, read_events=0, run_active=False,
    )  # fmt: skip
    assert no_watch["statement"] == (
        "Filesystem observation unavailable; final Git reconciliation completed. "
        "Agent telemetry unavailable; read activity not captured."
    )
    assert no_watch["level"] == "partial"
    crashed = run_completeness(
        adapter="demo", observers=[_obs(stopped=False)], has_start=True, has_end=True,
        is_repo=True, read_events=0, run_active=False,
    )  # fmt: skip
    assert crashed["writes"] == "partial"
    offline = run_completeness(
        adapter="herdr", observers=[_obs()], has_start=True, has_end=True, is_repo=True,
        read_events=0, run_active=False,
        offline=[OfflineWindow("2026-09-01T12:00:00+00:00", "2026-09-01T12:05:00+00:00")],
    )  # fmt: skip
    assert offline["statement"].startswith(
        "Partial: AgentTape was offline from 2026-09-01T12:00:00+00:00 to "
        "2026-09-01T12:05:00+00:00; final Git reconciliation still ran."
    )
    assert offline["level"] == "partial"
    pending = run_completeness(
        adapter="demo", observers=[_obs(stopped=False)], has_start=True, has_end=False,
        is_repo=True, read_events=1, run_active=True,
    )  # fmt: skip
    assert pending["level"] == "pending" and pending["git"] == "pending"


def test_an_observed_runs_statement_names_its_hooks_not_agent_telemetry() -> None:
    """An observed Claude Code session's reads reach AgentTape only through its hooks, so
    its statement says so, and says whether those hooks covered the whole session."""

    def statement(observed: ObservedReads) -> str:
        result = run_completeness(
            adapter="claude-code", observers=[_obs()], has_start=True, has_end=True,
            is_repo=True, read_events=1, run_active=False, observed_reads=observed,
        )  # fmt: skip
        assert result["reads"] == "telemetry"
        return str(result["statement"])

    assert statement(ObservedReads(hooks=True, gap_statement="")) == (
        "Writes and final changes fully observed; reads available only from Claude Code "
        "hooks, which AgentTape received for the whole session."
    )
    gap = "3 Claude Code hook events from this session were not recorded."
    assert statement(ObservedReads(hooks=True, gap_statement=gap)) == (
        "Writes and final changes fully observed; reads available only from Claude Code "
        f"hooks, which did not cover the whole session. {gap}"
    )
    none = "AgentTape received no Claude Code hooks from this session, so it saw none of its reads."
    assert statement(ObservedReads(hooks=False, gap_statement=none)) == (
        f"Writes and final changes fully observed. {none}"
    )


def test_a_hosted_session_covered_by_its_hooks_has_reads_even_with_no_read_event() -> None:
    """A Herdr run's adapter reports no reads, but a hosted Claude Code session's
    hooks report every tool call, so a session that read nothing still has read telemetry
    (from its hooks), never "Agent telemetry unavailable"."""
    covered = run_completeness(
        adapter="herdr", observers=[_obs()], has_start=True, has_end=True, is_repo=True,
        read_events=0, run_active=False,
        observed_reads=ObservedReads(hooks=True, gap_statement=""),
    )  # fmt: skip
    assert covered["reads"] == "telemetry"
    assert "Agent telemetry unavailable" not in covered["statement"]
    assert covered["statement"].endswith(
        "reads available only from Claude Code hooks, which AgentTape received for the "
        "whole session."
    )
    gap = (
        "AgentTape restarted while observing this session; hooks sent while it was down were lost."
    )
    gapped = run_completeness(
        adapter="herdr", observers=[_obs()], has_start=True, has_end=True, is_repo=True,
        read_events=0, run_active=False,
        observed_reads=ObservedReads(hooks=True, gap_statement=gap),
    )  # fmt: skip
    assert gapped["reads"] == "telemetry"
    assert "which did not cover the whole session." in gapped["statement"]
    assert gapped["statement"].endswith(gap)
    # A Herdr run no hook ever reached still has no read telemetry at all.
    bare = run_completeness(
        adapter="herdr", observers=[_obs()], has_start=True, has_end=True, is_repo=True,
        read_events=0, run_active=False,
    )  # fmt: skip
    assert bare["reads"] == "unavailable"
    assert "Agent telemetry unavailable" in bare["statement"]


# --------------------------------------------------------------------------- determinism / hashing


def test_summary_rebuild_is_deterministic_regardless_of_input_order() -> None:
    events = [
        ev("read", "a.py", "agent_hook", t=0, seq=1),
        ev("modify", "a.py", t=1, after=H_B, seq=2),
        ev("create", "b.py", t=2, after=H_C, seq=3),
        ev("rename", "c.py", "agent_hook", prev="b.py", t=3, seq=4),
        ev(
            "collapsed",
            "node_modules/",
            t=4,
            count=9,
            details={"by_operation": {"create": 9}},
            seq=5,
        ),
    ]
    start = snap({"a.py": [H_A, 1, False]})
    end = snap({"a.py": [H_B, 2, False], "b.py": ABSENT, "c.py": [H_C, 3, False]})
    baseline = build_summaries(events, start, end)
    for seed in range(5):
        shuffled = list(events)
        random.Random(seed).shuffle(shuffled)
        assert build_summaries(shuffled, start, end) == baseline
    assert [s.file_key for s in baseline] == ["a.py", "c.py", "node_modules/"]


def test_blob_hash_matches_git_and_hash_file_never_keeps_content(tmp_path: Path) -> None:
    assert blob_hash(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"  # git's empty blob
    f = tmp_path / "bin.dat"
    f.write_bytes(b"\0\1\2" * 10)
    state = hash_file(f)
    assert state is not None and state.binary is True and state.size == 30
    assert hash_file(tmp_path / "missing") is None
    big = hash_file(f, max_bytes=5)
    assert big is not None and big.hash is None and big.size == 30


@pytest.mark.parametrize("value", [None, [], [None, None, None]])
def test_absent_file_states(value: Any) -> None:
    assert FileState.from_list(value) is None


def test_collector_redacts_secret_values_resolved_for_the_attempt(tmp_path: Path) -> None:
    secret = "tape-run-secret-value-8812"  # not credential-shaped: patterns alone miss it
    c = _collector(tmp_path, redact=lambda t: t.replace(secret, "[REDACTED:secret]"))
    assert Redactor().redact_text(f"deploy --key {secret}") == f"deploy --key {secret}"
    assert c.redactor.redact_text(f"deploy --key {secret}") == "deploy --key [REDACTED:secret]"
    assert c.redactor.redact_value({"argv": ["deploy", secret]}) == {
        "argv": ["deploy", "[REDACTED:secret]"]
    }


def test_csv_export_neutralises_spreadsheet_formulas() -> None:
    from agenttape.file_evidence.service import _csv

    rows = [
        {"path": '=HYPERLINK("http://evil","x")', "command": "=cmd|' /C calc'!A0"},
        {"path": "@SUM(1+1)", "command": "-2+3"},
        {"path": "src/ok.py", "command": "pytest -q"},
    ]
    text = _csv(rows, ["path", "command"]).decode()
    cells = [c for line in text.splitlines()[1:] for c in line.split(",")]
    assert not any(c.lstrip('"').startswith(("=", "+", "-", "@")) for c in cells), text
    assert "src/ok.py,pytest -q" in text
