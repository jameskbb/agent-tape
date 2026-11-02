"""Worker-side File Evidence collection.

* :func:`start_file_evidence` (awaited by the agent executor before the adapter starts)
  - takes the Git start snapshot, then, for the rest of the attempt, runs a
  ``watchfiles`` observer on the run's workdir (the evidence boundary), turns new agent
  telemetry events into evidence about once a second, evaluates protected paths and the
  boundary live, persists everything continuously, and rebuilds the live summaries.
* :func:`reconcile_after_attempt` (after-attempt hook) - catches up on telemetry, takes
  the Git end snapshot, records Git end state and reconciliation findings (changes the
  watcher missed, exact renames), and rebuilds the summaries.

The watcher proves that a path changed, never that it was read. Noisy directories are
counted per directory and never hashed. The pending set is bounded: past the limit,
events are counted as dropped and the run's evidence is marked partial.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import os
import threading
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from agenttape.adapters.base import AdapterResult, EventEmitter, ExecutionContext
from agenttape.application.context import AppContext
from agenttape.domain.enums import ApprovalStatus, EventType, FailureCategory, Severity
from agenttape.domain.policy import matches_protected
from agenttape.file_evidence import store
from agenttape.file_evidence.ledger import (
    WRITE_OPS,
    EventRow,
    EvidenceDraft,
    FileState,
    NoisePolicy,
    hash_file,
    is_temp_save,
    rename_pairs,
    telemetry_evidence,
)
from agenttape.file_evidence.snapshot import end_snapshot, start_snapshot
from agenttape.infrastructure.db.models import ApprovalRequest, Event, Run
from agenttape.security.redaction import RedactionReport, Redactor
from agenttape.worker.evidence_feed import EventFeed, feed_for

FLUSH_SECONDS = 1.0
REBUILD_SECONDS = 2.0
WATCH_DEBOUNCE_MS = 300
FINAL_GRACE_SECONDS = 0.4
#: How long a stopping watcher may take to deliver its last batch (one debounce + timeout).
WATCH_TIMEOUT_MS = 500
FINAL_DRAIN_SECONDS = 2.0
MAX_PENDING_PATHS = 5000
TELEMETRY_BATCH = 500
TELEMETRY_TYPES = ("file_read", "file_write", "tool_call", "command")
#: A rebuild reads the run's whole ledger; live rebuilds take at most ~1/N of wall time.
REBUILD_BUDGET_FACTOR = 5
RECONCILED_SOURCES = ("git_end", "reconciliation")


def _settings(ec: ExecutionContext) -> dict[str, Any]:
    return store.project_settings(dict(ec.run.snapshot))


def _fe_settings(ec: ExecutionContext) -> dict[str, Any]:
    return dict(_settings(ec).get("file_evidence") or {})


def _runtime(ec: ExecutionContext) -> str:
    return "herdr" if ec.adapter_type == "herdr" else "local"


@dataclass
class _Pending:
    first: datetime
    last: datetime
    ops: list[str] = field(default_factory=list)
    #: The file's state when the latest batch arrived ("dir" for directories; None when
    #: gone), and every distinct content hash seen (intermediate states prove a restore).
    state: FileState | str | None = None
    state_known: bool = False
    hashes: list[str] = field(default_factory=list)


MAX_OBSERVED_HASHES = 8


def _states(root: Path, rels: list[str]) -> dict[str, FileState | str | None]:
    out: dict[str, FileState | str | None] = {}
    for rel in rels:
        full = root / rel
        out[rel] = "dir" if full.is_dir() else hash_file(full)
    return out


class AttemptRedactor(Redactor):
    """Pattern redaction plus the attempt's own redactor (resolved secret values)."""

    def __init__(self, redact: Callable[[str], str]) -> None:
        super().__init__()
        self._attempt_redact = redact

    def redact_text(self, text: str, report: RedactionReport | None = None) -> str:
        return super().redact_text(self._attempt_redact(text) if text else text, report)


class Collector:
    """Evidence collection for one attempt (the monitor and the after-hook share it)."""

    def __init__(self, ctx: AppContext, ec: ExecutionContext, system: EventEmitter) -> None:
        assert ec.workdir is not None
        self.ctx = ctx
        self.ec = ec
        self.system = system
        self.root = Path(ec.workdir).resolve()
        self.settings = _settings(ec)
        self.fe = _fe_settings(ec)
        self.noise = NoisePolicy.from_settings(self.settings)
        self.patterns = [
            str(p) for p in self.settings.get("protected_paths") or [] if isinstance(p, str)
        ]
        # The attempt's redactor knows the secret values resolved for this run; the pattern
        # redactor alone would persist a resolved secret that appears in command text.
        self.redactor: Redactor = AttemptRedactor(ec.redact)
        self.session_id = uuid.uuid4().hex[:12]
        self.pending: dict[str, _Pending] = {}
        self.collapsed: dict[str, Counter[str]] = {}
        self.collapsed_times: dict[str, tuple[datetime, datetime]] = {}
        self.dropped = 0
        self.flushes = 0
        self.last_seq = 0
        self.feed: EventFeed | None = None
        self.telemetry: list[EvidenceDraft] = []
        self.last_hash: dict[str, str | None] = {}
        self.policy_cache: dict[str, dict[str, Any]] = {}
        self.dirty_since_rebuild = False
        self.last_rebuild = 0.0
        self.rebuild_interval = REBUILD_SECONDS
        self.node_key: str | None = None
        self.observer_status = "unavailable"
        self.observer_reason: str | None = None
        # stop_flag ends the loop after the current batch is delivered; abort_flag makes the
        # OS watcher return at once (it then drops the batch it was debouncing).
        self.stop_flag = threading.Event()
        self.abort_flag = threading.Event()
        #: Set once the start snapshot is taken and the watcher is live (or failed to start).
        self.ready = asyncio.Event()

    @property
    def runtime(self) -> str:
        # Read live: a Herdr attempt that fell back to local before starting relabels ec.
        return _runtime(self.ec)

    # ------------------------------------------------------------------ setup

    def _prepare(self) -> None:
        """Start snapshot (first attempt entry only), node attribution, telemetry cursor."""
        now = self.ctx.clock.now()
        with self.ctx.session() as session:
            snap = store.get_snapshot(session, self.ec.attempt.id, "start")
            if snap is None:
                data = start_snapshot(self.root, self.noise)
                data.setdefault("details", {})["noise_policy"] = self.noise.as_dict()
                data["details"]["boundary"] = "run workdir"
                snap = store.save_snapshot(
                    session,
                    run_id=self.ec.run.id,
                    attempt_id=self.ec.attempt.id,
                    kind="start",
                    data=data,
                    taken_at=now,
                    replace=False,
                )
            self.last_hash = {p: (v[0] if v else None) for p, v in (snap.files or {}).items()}
            run = session.get(Run, self.ec.run.id)
            if run is not None and run.parent_node_key:
                # Only an isolated workdir belongs to exactly one workflow step.
                from agenttape.infrastructure.db.extra_models.git_workspaces import RunWorkspace

                ws = session.scalars(
                    select(RunWorkspace).where(RunWorkspace.run_id == run.id)
                ).first()
                if ws is not None and ws.isolated:
                    self.node_key = run.parent_node_key
            self.last_seq = store.last_processed_event_sequence(
                session, self.ec.run.id, self.ec.attempt.id
            )

    def _record_observer(self, *, stopped: bool = False) -> None:
        observer: dict[str, Any] = {
            "session": self.session_id,
            "status": self.observer_status,
            "reason": self.observer_reason,
            "dropped": self.dropped,
            "debounce_ms": WATCH_DEBOUNCE_MS,
            "max_pending_paths": MAX_PENDING_PATHS,
        }
        if not stopped:
            observer["started_at"] = self.ctx.clock.now().isoformat()
        else:
            observer["stopped_at"] = self.ctx.clock.now().isoformat()
        with self.ctx.session() as session:
            store.update_observer(session, self.ec.attempt.id, observer)

    # ------------------------------------------------------------------ watcher

    def open_watcher(self) -> Any:
        """Create the OS watcher (inotify/FSEvents/...); it is live once this returns."""
        from watchfiles._rust_notify import RustNotify

        return RustNotify([str(self.root)], False, False, 300, True, True)

    async def watch(self, notify: Any) -> None:
        """Drain watcher batches until stopped (``self.stop_flag``)."""
        ops = {1: "create", 2: "modify", 3: "delete"}  # watchfiles.Change values
        root = str(self.root)
        try:
            while not self.stop_flag.is_set():
                changes = await asyncio.to_thread(
                    notify.watch, WATCH_DEBOUNCE_MS, 50, WATCH_TIMEOUT_MS, self.abort_flag
                )
                if not isinstance(changes, set):
                    continue  # "timeout" / "stop" / "signal"
                now = self.ctx.clock.now()
                batch: list[str] = []
                for change, path in sorted(changes, key=lambda c: (c[1], c[0])):
                    rel = os.path.relpath(path, root).replace(os.sep, "/")
                    if self.observe(ops.get(int(change), "modify"), rel, now):
                        batch.append(rel)
                if batch:
                    # Hash while the batch is fresh, so intermediate content is evidence.
                    self.record_states(await asyncio.to_thread(_states, self.root, batch))
        except Exception as exc:  # inotify limits, vanished directory, ...
            self.observer_status = "degraded"
            self.observer_reason = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            with contextlib.suppress(Exception):
                notify.close()

    def record_states(self, states: dict[str, FileState | str | None]) -> None:
        for rel, state in states.items():
            entry = self.pending.get(rel)
            if entry is None:
                continue
            entry.state, entry.state_known = state, True
            if isinstance(state, FileState) and state.hash and state.hash not in entry.hashes:
                entry.hashes = [*entry.hashes, state.hash][-MAX_OBSERVED_HASHES:]

    def observe(self, op: str, rel: str, now: datetime) -> bool:
        """Record one watcher change (bounded). True when tracked individually."""
        if rel.startswith("..") or rel in ("", ".") or self.noise.is_git_internal(rel):
            return False
        bucket = self.noise.collapse_bucket(rel)
        if bucket is not None:
            self.collapsed.setdefault(bucket, Counter())[op] += 1
            first, _last = self.collapsed_times.get(bucket, (now, now))
            self.collapsed_times[bucket] = (first, now)
            return False
        entry = self.pending.get(rel)
        if entry is None:
            if len(self.pending) >= MAX_PENDING_PATHS:
                self.dropped += 1
                return False
            entry = self.pending[rel] = _Pending(first=now, last=now)
        entry.ops.append(op)
        entry.last = now
        return True

    # ------------------------------------------------------------------ flush

    def _watch_drafts(
        self, pending: dict[str, _Pending], states: dict[str, FileState | str | None]
    ) -> list[EvidenceDraft]:
        drafts: list[EvidenceDraft] = []
        self.flushes += 1
        tag = f"{self.session_id}:{self.flushes}"
        # Temporary-save files created and removed within one batch fold into their target.
        folded: dict[str, str] = {}
        for rel, p in pending.items():
            name = rel.rsplit("/", 1)[-1]
            if states.get(rel) is None and "create" in p.ops and is_temp_save(name):
                parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
                targets = [
                    t
                    for t in pending
                    if t != rel
                    and (t.rsplit("/", 1)[0] if "/" in t else "") == parent
                    and t.rsplit("/", 1)[-1] in name
                    and isinstance(states.get(t), FileState)
                ]
                if len(targets) == 1:
                    folded[rel] = targets[0]
        # Rename candidates: paths that vanished, including ones created earlier in this
        # flush window (known only by the hash an earlier watcher batch recorded).
        deleted = [
            r
            for r, p in pending.items()
            if states.get(r) is None
            and r not in folded
            and (r in self.last_hash or ("create" in p.ops and p.hashes))
        ]
        added = [
            r
            for r, p in pending.items()
            if isinstance(states.get(r), FileState) and r not in self.last_hash
        ]

        def old_hash(r: str) -> str | None:
            if r in self.last_hash:
                return self.last_hash[r]
            return pending[r].hashes[-1] if pending[r].hashes else None

        def new_hash(r: str) -> str | None:
            s = states.get(r)
            return s.hash if isinstance(s, FileState) else None

        renames = dict((new, old) for old, new in rename_pairs(deleted, added, old_hash, new_hash))
        renamed_from = set(renames.values())
        #: Created and renamed away within this window: recorded as a create, then the rename.
        born = {old for old in renamed_from if old not in self.last_hash}
        for rel in sorted(pending):
            p = pending[rel]
            state = states.get(rel)
            if rel in folded or (rel in renamed_from and rel not in born) or state == "dir":
                continue
            if state is None and rel not in self.last_hash and "create" not in p.ops:
                continue  # an unknown path vanished: a directory or an ignored file
            existed = rel in self.last_hash and self.last_hash.get(rel) is not None
            ops: list[str] = []
            for op in p.ops:
                if op == "create" and existed and not ops:
                    op = "modify"  # atomic replace of an existing file
                if not ops or ops[-1] != op:
                    ops.append(op)
            counts = Counter(p.ops)
            if rel in renames:
                ops = ["rename"] + [o for o in ops if o == "modify"]
            elif rel in born:
                ops = ["create"]
            if isinstance(state, FileState) and ops and ops[-1] == "delete":
                ops.append("create")  # deleted and recreated
            for i, op in enumerate(ops):
                last = i == len(ops) - 1
                d = EvidenceDraft(
                    operation=op,
                    path=rel,
                    previous_path=renames.get(rel) if op == "rename" else None,
                    dedupe_key=f"fs:{tag}:{rel}:{i}:{op}",
                    count=max(1, counts.get(op, 1)) if op != "rename" else 1,
                    after_hash=state.hash if last and isinstance(state, FileState) else None,
                    size_bytes=state.size if last and isinstance(state, FileState) else None,
                    is_binary=state.binary if last and isinstance(state, FileState) else None,
                    details={
                        "raw_operations": dict(counts),
                        **({"observed_hashes": p.hashes} if len(p.hashes) > 1 else {}),
                        **(
                            {"via_temp": [t for t, g in folded.items() if g == rel]}
                            if rel in folded.values()
                            else {}
                        ),
                        **({"recreated": True} if "delete" in ops and op == "create" and i else {}),
                    },
                    source="filesystem_watcher",
                    quality="observed",
                    ts=p.first,
                    last_ts=p.last,
                    attempt_id=self.ec.attempt.id,
                    node_key=self.node_key,
                    runtime=self.runtime,
                    before_hash=self.last_hash.get(rel) if i == 0 else None,
                )
                drafts.append(d)
            self.last_hash[rel] = state.hash if isinstance(state, FileState) else None
        for old in renamed_from:
            self.last_hash.pop(old, None)
        return drafts

    def _collapsed_drafts(
        self, collapsed: dict[str, Counter[str]], times: dict[str, tuple[datetime, datetime]]
    ) -> list[EvidenceDraft]:
        tag = f"{self.session_id}:{self.flushes}"
        out = []
        for bucket in sorted(collapsed):
            counts = collapsed[bucket]
            first, last = times[bucket]
            out.append(
                EvidenceDraft(
                    operation="collapsed",
                    path=bucket,
                    source="filesystem_watcher",
                    quality="observed",
                    ts=first,
                    last_ts=last,
                    count=sum(counts.values()),
                    attempt_id=self.ec.attempt.id,
                    node_key=self.node_key,
                    runtime=self.runtime,
                    dedupe_key=f"fs:{tag}:collapsed:{bucket}",
                    details={"by_operation": dict(sorted(counts.items())), "policy": "noise"},
                )
            )
        return out

    def _telemetry_drafts(self) -> list[EvidenceDraft]:
        """Drafts from the attempt's shared evidence feed (one read serves File and Context
        Evidence; a flush forces it so the final flush sees everything persisted)."""
        if self.feed is None:
            self.feed = feed_for(self.ctx, self.ec.run.id, self.ec.attempt.id)
            self.feed.subscribe(TELEMETRY_TYPES, self._on_events, after=self.last_seq)
        self.feed.poll(force=True)
        with self.feed.lock:
            drafts, self.telemetry = self.telemetry, []
        return drafts

    def _on_events(self, _session: Any, events: list[Event]) -> None:
        for e in events:
            self.last_seq = max(self.last_seq, e.sequence)
            self.telemetry.extend(
                telemetry_evidence(
                    EventRow(
                        id=e.id,
                        sequence=e.sequence,
                        ts=e.ts,
                        type=e.type,
                        source=e.source,
                        payload=e.payload or {},
                        attempt_id=e.attempt_id,
                        node_key=e.node_key or self.node_key,
                    ),
                    self.root,
                    is_file=lambda rel: (self.root / rel).is_file(),
                    runtime=self.runtime,
                )
            )

    async def flush(self) -> int:
        """Persist everything observed since the last flush. Returns rows inserted."""
        pending, self.pending = self.pending, {}
        collapsed, self.collapsed = self.collapsed, {}
        times, self.collapsed_times = self.collapsed_times, {}
        drafts: list[EvidenceDraft] = []
        more = True
        while more:
            batch = await asyncio.to_thread(self._telemetry_drafts)
            drafts.extend(batch)
            more = len(batch) >= TELEMETRY_BATCH
            if not batch:
                break
        if pending:
            unknown = [rel for rel, p in pending.items() if not p.state_known]
            fresh = await asyncio.to_thread(_states, self.root, unknown) if unknown else {}
            states = {rel: fresh.get(rel, p.state) for rel, p in pending.items()}
            drafts.extend(self._watch_drafts(pending, states))
        if collapsed:
            drafts.extend(self._collapsed_drafts(collapsed, times))
        if not drafts:
            return 0
        drafts.sort(key=lambda d: d.ts)
        for d in drafts:
            await self._apply_policy(d)
        rows = await asyncio.to_thread(self._insert, drafts)
        if rows:
            self.dirty_since_rebuild = True
        return rows

    def _insert(self, drafts: list[EvidenceDraft]) -> int:
        with self.ctx.session() as session:
            return len(
                store.insert_evidence(session, self.ec.run.id, drafts, redactor=self.redactor)
            )

    def rebuild(self) -> None:
        with self.ctx.session() as session:
            store.rebuild_summaries(
                session, self.ec.run.id, now=self.ctx.clock.now(), live_attempt=self.ec.attempt.id
            )
        self.dirty_since_rebuild = False

    # ------------------------------------------------------------------ policy

    def _approved_for(self, paths: list[str]) -> str | None:
        with self.ctx.session() as session:
            rows = session.scalars(
                select(ApprovalRequest).where(
                    ApprovalRequest.run_id == self.ec.run.id,
                    ApprovalRequest.status == ApprovalStatus.APPROVED.value,
                )
            ).all()
            for a in rows:
                action = a.requested_action or {}
                declared = set(action.get("paths") or []) | set(action.get("files") or [])
                if any(p in declared for p in paths):
                    return a.id
        return None

    async def _apply_policy(self, d: EvidenceDraft) -> None:
        if d.operation not in WRITE_OPS and d.inside_boundary:
            return
        if not d.inside_boundary:
            await self._outside(d)
            return
        paths = [d.path] + ([d.previous_path] if d.previous_path else [])
        hits = sorted({m for p in paths for m in matches_protected(p, self.patterns)})
        if not hits:
            return
        d.protected_patterns = hits
        cached = self.policy_cache.get(d.path)
        if cached is not None:
            d.policy = cached
            return
        approval_id = await asyncio.to_thread(self._approved_for, paths)
        if approval_id is not None:
            policy: dict[str, Any] = {
                "kind": "protected_path",
                "decision": "approved",
                "approval_id": approval_id,
                "patterns": hits,
                "detected_by": d.source,
            }
            await self.system.event(
                EventType.FILE_EVIDENCE,
                f"Protected path written after approval: {d.path}",
                {"kind": "protected_path", "path": d.path, "policy": policy},
                severity=Severity.NOTICE,
            )
        else:
            cancel = self.fe.get("cancel_on_protected_write") is True
            policy = {
                "kind": "protected_path",
                "decision": "detected",
                "prevented": False,
                "review_required": True,
                "cancelled": cancel,
                "patterns": hits,
                "detected_by": d.source,
            }
            await self.system.event(
                EventType.FILE_EVIDENCE,
                f"Protected path changed (detected, not prevented): {d.path}",
                {"kind": "protected_path", "path": d.path, "policy": policy},
                severity=Severity.ERROR,
            )
            if cancel:
                self.ec.cancel.cancel(
                    FailureCategory.POLICY_VIOLATION,
                    f"Protected path {d.path} was changed without approval",
                )
        self.policy_cache[d.path] = policy
        d.policy = policy

    async def _outside(self, d: EvidenceDraft) -> None:
        key = f"outside:{d.path}:{d.operation}"
        if key in self.policy_cache:
            d.policy = self.policy_cache[key]
            return
        write = d.operation in WRITE_OPS
        cancel = write and self.fe.get("cancel_on_outside_write") is not False
        policy = {
            "kind": "outside_boundary",
            "decision": "detected",
            "prevented": False,
            "cancelled": cancel,
            "operation": d.operation,
            "detected_by": d.source,
        }
        await self.system.event(
            EventType.FILE_EVIDENCE,
            f"Outside-boundary {d.operation} {'attempt' if write else 'reference'}: {d.path}",
            {"kind": "outside_boundary", "path": d.path, "policy": policy},
            severity=Severity.ERROR if write else Severity.WARNING,
        )
        if cancel:
            self.ec.cancel.cancel(
                FailureCategory.POLICY_VIOLATION,
                f"File operation outside the project boundary: {d.operation} {d.path}",
            )
        self.policy_cache[key] = policy
        d.policy = policy

    # ------------------------------------------------------------------ monitor loop

    async def run(self) -> None:
        watcher: asyncio.Task[None] | None = None
        try:
            await asyncio.to_thread(self._prepare)
            if self.fe.get("watcher", True) is not False and self.root.is_dir():
                try:
                    notify = await asyncio.to_thread(self.open_watcher)
                except Exception as exc:
                    self.observer_reason = f"{type(exc).__name__}: {exc}"[:300]
                else:
                    self.observer_status = "active"
                    watcher = asyncio.create_task(self.watch(notify))
            else:
                self.observer_reason = (
                    "disabled by project policy" if self.root.is_dir() else "workdir missing"
                )
            await asyncio.to_thread(self._record_observer)
        finally:
            self.ready.set()  # the agent may start now (or evidence failed to start; never block)
        loop = asyncio.get_running_loop()
        try:
            while True:
                await asyncio.sleep(FLUSH_SECONDS)
                await self.flush()
                if (
                    self.dirty_since_rebuild
                    and loop.time() - self.last_rebuild >= self.rebuild_interval
                ):
                    began = loop.time()
                    await asyncio.to_thread(self.rebuild)
                    self.last_rebuild = loop.time()
                    self.rebuild_interval = max(
                        REBUILD_SECONDS, REBUILD_BUDGET_FACTOR * (self.last_rebuild - began)
                    )
        finally:
            # Normal shutdown: let the watcher deliver the batch it is debouncing (a file
            # written just before the agent exited), then flush what remains. Abort the OS
            # watcher only if that takes longer than the bound.
            with contextlib.suppress(Exception):
                if watcher is not None and not watcher.done():
                    await asyncio.sleep(FINAL_GRACE_SECONDS)
                    self.stop_flag.set()
                    try:
                        await asyncio.wait_for(asyncio.shield(watcher), timeout=FINAL_DRAIN_SECONDS)
                    except TimeoutError:
                        self.abort_flag.set()
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(watcher, timeout=2.0)
                await self.flush()
                if self.dropped:
                    self.observer_status = "degraded"
                    self.observer_reason = (
                        f"{self.dropped} filesystem events were not recorded individually "
                        f"(more than {MAX_PENDING_PATHS} distinct paths per flush)"
                    )
                    await self.system.event(
                        EventType.FILE_EVIDENCE,
                        f"File evidence degraded: {self.dropped} watcher events not recorded",
                        {"kind": "observer_degraded", "dropped": self.dropped},
                        severity=Severity.WARNING,
                    )
                await asyncio.to_thread(self._record_observer, stopped=True)


READY_TIMEOUT_SECONDS = 30.0


async def start_file_evidence(
    ctx: AppContext, ec: ExecutionContext, system: EventEmitter
) -> asyncio.Task[None] | None:
    """Start collection for the attempt; returns once the start snapshot is taken and the
    watcher is live (bounded), so no early change is missed or baked into the "start"
    state. The returned task runs for the attempt; the executor cancels it at the end."""
    if ec.workdir is None:
        return None
    try:
        collector = Collector(ctx, ec, system)
    except Exception:  # never break a run because evidence could not start
        return None
    task = asyncio.create_task(_collect(collector))
    try:
        await asyncio.wait_for(asyncio.shield(collector.ready.wait()), READY_TIMEOUT_SECONDS)
    except TimeoutError:
        with contextlib.suppress(Exception):
            await system.event(
                EventType.FILE_EVIDENCE,
                "File evidence was not ready in time; the agent started anyway",
                {"kind": "observer_late", "timeout_seconds": READY_TIMEOUT_SECONDS},
                severity=Severity.WARNING,
            )
    return task


async def _collect(collector: Collector) -> None:
    try:
        await collector.run()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        collector.ready.set()
        with contextlib.suppress(Exception):
            await collector.system.event(
                EventType.FILE_EVIDENCE,
                f"File evidence collection stopped: {type(exc).__name__}: {exc}"[:300],
                {"kind": "collector_error"},
                severity=Severity.WARNING,
            )


# --------------------------------------------------------------------------- after attempt


def reconcile_run(
    ctx: AppContext,
    *,
    run_id: str,
    attempt_id: str,
    root: Path,
    noise: NoisePolicy,
    node_key: str | None = None,
    runtime: str | None = None,
    redactor: Redactor | None = None,
) -> dict[str, Any]:
    """End snapshot + Git end / reconciliation evidence + summary rebuild (sync).

    Idempotent: evidence keys include the attempt and the final content hash, so running
    it again over an unchanged directory adds nothing.
    """
    now = ctx.clock.now()
    with ctx.session() as session:
        start_row, _end, _starts = store.run_snapshots(session, run_id, files=True)
        index = store.evidence_index(session, run_id)
    start_files = dict(start_row.files or {}) if start_row is not None else {}
    start_dirty = list((start_row.details or {}).get("dirty_files") or []) if start_row else []
    evidence_paths = {p for p, _op, _prev, _src in index} | {
        prev for _p, _op, prev, _src in index if prev
    }
    data = end_snapshot(
        root,
        noise,
        base=start_row.head if start_row is not None else None,
        evidence_paths=evidence_paths,
        start_files=start_files,
        start_dirty=start_dirty,
    )
    end_files: dict[str, list[Any]] = dict(data.get("files") or {})
    drafts: list[EvidenceDraft] = []
    # Only live evidence counts: an earlier (provisional) reconciliation is recomputed.
    live = [(p, op, prev) for p, op, prev, src in index if src not in RECONCILED_SOURCES]
    renamed_from = {prev for _p, op, prev in live if op == "rename" and prev}
    observed_writes = {p for p, op, _prev in live if op in WRITE_OPS} | renamed_from
    start_snap = store.as_snapshot(start_row)
    if start_row is not None and start_snap is not None:
        start_state = {p: FileState.from_list(v) for p, v in start_files.items()}
        end_state = {p: FileState.from_list(v) for p, v in end_files.items()}
        vanished = [
            p
            for p, s in start_state.items()
            if s is not None and p in end_state and end_state[p] is None
        ]
        appeared = [p for p, s in end_state.items() if s is not None and start_state.get(p) is None]
        pairs = [
            (old, new)
            for old, new in rename_pairs(
                vanished,
                appeared,
                lambda p: (start_state.get(p) or FileState(None, None, None)).hash,
                lambda p: (end_state.get(p) or FileState(None, None, None)).hash,
            )
            if old not in renamed_from
        ]
        paired_old = {o for o, _n in pairs}
        paired_new = {n for _o, n in pairs}
        head = (data.get("head") or "")[:12]
        for p, e in sorted(end_state.items()):
            s = start_state.get(p)
            if p in paired_old or p in paired_new:
                continue
            if s is None and e is None:
                continue
            if s is not None and e is not None and s.hash == e.hash and s.hash is not None:
                continue
            if s is not None and e is not None and s.hash is None and s.size == e.size:
                continue
            op = "create" if s is None else "delete" if e is None else "modify"
            if s is None and not start_snap.knows(p) and not start_snap.absent_known(p):
                continue  # ignored at start: its starting state is unknown
            key_hash = e.hash if e is not None else "absent"
            drafts.append(
                EvidenceDraft(
                    operation=op,
                    path=p,
                    source="git_end",
                    quality="reconciled",
                    ts=now,
                    dedupe_key=f"git_end:{attempt_id}:{p}:{key_hash}",
                    attempt_id=attempt_id,
                    node_key=node_key,
                    runtime=runtime,
                    before_hash=s.hash if s else None,
                    after_hash=e.hash if e else None,
                    size_bytes=e.size if e else None,
                    is_binary=e.binary if e else None,
                    details={
                        "head": head,
                        "numstat": (data.get("details") or {}).get("numstat", {}).get(p),
                    },
                )
            )
            if p not in observed_writes:
                drafts.append(
                    EvidenceDraft(
                        operation=op,
                        path=p,
                        source="reconciliation",
                        quality="reconciled",
                        ts=now,
                        dedupe_key=f"recon:{attempt_id}:{p}:{op}:{key_hash}",
                        attempt_id=attempt_id,
                        node_key=node_key,
                        runtime=runtime,
                        before_hash=s.hash if s else None,
                        after_hash=e.hash if e else None,
                        details={
                            "finding": "changed_without_watcher_event",
                            "note": "Git shows a final change that live observation did not report.",
                        },
                    )
                )
        for old, new in pairs:
            e = end_state.get(new)
            drafts.append(
                EvidenceDraft(
                    operation="rename",
                    path=new,
                    previous_path=old,
                    source="reconciliation",
                    quality="reconciled",
                    ts=now,
                    dedupe_key=f"recon:{attempt_id}:{old}->{new}",
                    attempt_id=attempt_id,
                    node_key=node_key,
                    runtime=runtime,
                    before_hash=(start_state.get(old) or FileState(None, None, None)).hash,
                    after_hash=e.hash if e else None,
                    details={"finding": "exact_rename_by_content_hash"},
                )
            )
    with ctx.session() as session:
        store.save_snapshot(
            session,
            run_id=run_id,
            attempt_id=attempt_id,
            kind="end",
            data=data,
            taken_at=now,
            replace=True,
        )
        # Policy for final protected changes nobody saw live is a summary flag (the Git
        # track's warning event already names them); do not open duplicate reviews.
        store.supersede_reconciliation(
            session, run_id, attempt_id, keep={d.dedupe_key for d in drafts}
        )
        inserted = store.insert_evidence(session, run_id, drafts, redactor=redactor)
        rows = store.rebuild_summaries(session, run_id, now=now)
        run = session.get(Run, run_id)
        status = store.completeness(session, run, reconciling=True) if run is not None else {}
    counts = Counter(r.classification for r in rows if not r.is_collapsed)
    missed = sum(1 for d in inserted if d.source == "reconciliation")
    return {
        "files": sum(1 for r in rows if not r.is_collapsed),
        "collapsed": sum(r.collapsed_count or 0 for r in rows if r.is_collapsed),
        "classifications": dict(sorted(counts.items())),
        "missed_by_watcher": missed,
        "completeness": status.get("statement"),
        "level": status.get("level"),
    }


async def reconcile_after_attempt(
    ctx: AppContext, ec: ExecutionContext, result: AdapterResult
) -> AdapterResult:
    if ec.workdir is None or not Path(ec.workdir).is_dir():
        return result
    try:
        collector = Collector(ctx, ec, ec.events)
        await asyncio.to_thread(collector._prepare)
        await collector.flush()  # telemetry the monitor had not processed yet
        outcome = await asyncio.to_thread(
            functools.partial(
                reconcile_run,
                ctx,
                run_id=ec.run.id,
                attempt_id=ec.attempt.id,
                root=collector.root,
                noise=collector.noise,
                node_key=collector.node_key,
                runtime=collector.runtime,
                redactor=collector.redactor,
            )
        )
    except Exception as exc:
        await ec.events.event(
            EventType.FILE_EVIDENCE,
            f"File evidence reconciliation failed: {type(exc).__name__}: {exc}"[:300],
            {"kind": "reconciliation_error"},
            severity=Severity.WARNING,
        )
        return result
    label = "provisional (paused for approval)" if result.outcome == "needs_approval" else "final"
    await ec.events.event(
        EventType.FILE_EVIDENCE,
        f"File evidence reconciled ({label}): {outcome['files']} file"
        f"{'' if outcome['files'] == 1 else 's'}"
        + (
            f", {outcome['missed_by_watcher']} found only by Git"
            if outcome["missed_by_watcher"]
            else ""
        )
        + (f", {outcome['collapsed']:,} collapsed events" if outcome["collapsed"] else ""),
        {"kind": "reconciliation", "stage": label, **outcome},
        severity=Severity.INFO,
    )
    return result
