"""Persistence for File Evidence: idempotent evidence inserts, snapshots, summary rebuilds.

All functions are synchronous and take a SQLAlchemy session (callers in the event loop
use ``asyncio.to_thread``). Summaries are always rebuilt wholesale from the raw evidence
and snapshots, in one transaction, so a rebuild is deterministic and repeatable.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, defer

from agenttape.domain.enums import TERMINAL_STATUS_VALUES, EventType, RunStatus
from agenttape.file_evidence.ledger import (
    CommandWindow,
    EvidenceDraft,
    LedgerEvent,
    ObservedReads,
    ObserverWindow,
    OfflineWindow,
    Snapshot,
    Summary,
    build_summaries,
    run_completeness,
)
from agenttape.findings.coverage import observed_coverage
from agenttape.findings.verify import observed_gaps, observed_statement
from agenttape.infrastructure.db.extra_models.file_evidence import (
    FileEvidenceEvent,
    FileEvidenceSnapshot,
    RunFileSummary,
)
from agenttape.infrastructure.db.models import Event, Run, RunAttempt
from agenttape.security.redaction import Redactor

KEY_CHUNK = 500


def protected_patterns(run: Run) -> list[str]:
    settings = ((run.snapshot or {}).get("project") or {}).get("settings") or {}
    return [str(p) for p in settings.get("protected_paths") or [] if isinstance(p, str)]


def project_settings(snapshot: dict[str, Any]) -> dict[str, Any]:
    return dict(((snapshot or {}).get("project") or {}).get("settings") or {})


# --------------------------------------------------------------------------- evidence


def insert_evidence(
    session: Session,
    run_id: str,
    drafts: Sequence[EvidenceDraft],
    *,
    redactor: Redactor | None = None,
) -> list[FileEvidenceEvent]:
    """Append drafts whose ``dedupe_key`` is new for the run; returns the inserted rows."""
    if not drafts:
        return []
    keys = [d.dedupe_key for d in drafts]
    existing: set[str] = set()
    for i in range(0, len(keys), KEY_CHUNK):
        chunk = keys[i : i + KEY_CHUNK]
        existing |= set(
            session.scalars(
                select(FileEvidenceEvent.dedupe_key).where(
                    FileEvidenceEvent.run_id == run_id, FileEvidenceEvent.dedupe_key.in_(chunk)
                )
            )
        )
    seq = int(
        session.scalar(
            select(func.max(FileEvidenceEvent.sequence)).where(FileEvidenceEvent.run_id == run_id)
        )
        or 0
    )
    rows: list[FileEvidenceEvent] = []
    for d in drafts:
        if d.dedupe_key in existing:
            continue
        existing.add(d.dedupe_key)
        seq += 1
        command = d.command
        details = d.details
        redacted = False
        if redactor is not None:
            if command:
                red = redactor.redact_text(command)
                redacted = red != command
                command = red
            if details:
                red_details = redactor.redact_value(details)
                redacted = redacted or red_details != details
                details = red_details
        row = FileEvidenceEvent(
            run_id=run_id,
            attempt_id=d.attempt_id,
            node_key=d.node_key,
            sequence=seq,
            ts=d.ts,
            last_ts=d.last_ts,
            operation=d.operation,
            path=d.path[:4096],
            previous_path=d.previous_path,
            inside_boundary=d.inside_boundary,
            source=d.source,
            quality=d.quality,
            adapter=d.adapter,
            runtime=d.runtime,
            tool=(d.tool or None) and str(d.tool)[:200],
            command=command,
            correlation_id=(d.correlation_id or None) and str(d.correlation_id)[:100],
            source_event_id=d.source_event_id,
            before_hash=d.before_hash,
            after_hash=d.after_hash,
            size_bytes=d.size_bytes,
            is_binary=d.is_binary,
            count=d.count,
            protected_patterns=list(d.protected_patterns),
            policy=d.policy,
            redacted=redacted,
            details=details,
            dedupe_key=d.dedupe_key[:255],
        )
        session.add(row)
        rows.append(row)
    session.flush()
    return rows


def last_processed_event_sequence(session: Session, run_id: str, attempt_id: str) -> int:
    """Highest ``events.sequence`` already turned into evidence for this attempt."""
    value = session.scalar(
        select(func.max(Event.sequence)).where(
            Event.run_id == run_id,
            Event.attempt_id == attempt_id,
            Event.id.in_(
                select(FileEvidenceEvent.source_event_id).where(
                    FileEvidenceEvent.run_id == run_id,
                    FileEvidenceEvent.source_event_id.is_not(None),
                )
            ),
        )
    )
    return int(value or 0)


def evidence_index(session: Session, run_id: str) -> list[tuple[str, str, str | None, str]]:
    """(path, operation, previous_path, source) for every evidence row of a run (light)."""
    return [
        (r[0], r[1], r[2], r[3])
        for r in session.execute(
            select(
                FileEvidenceEvent.path,
                FileEvidenceEvent.operation,
                FileEvidenceEvent.previous_path,
                FileEvidenceEvent.source,
            ).where(FileEvidenceEvent.run_id == run_id)
        )
    ]


# --------------------------------------------------------------------------- snapshots


def get_snapshot(session: Session, attempt_id: str, kind: str) -> FileEvidenceSnapshot | None:
    return session.scalars(
        select(FileEvidenceSnapshot).where(
            FileEvidenceSnapshot.attempt_id == attempt_id, FileEvidenceSnapshot.kind == kind
        )
    ).first()


def save_snapshot(
    session: Session,
    *,
    run_id: str,
    attempt_id: str,
    kind: str,
    data: dict[str, Any],
    taken_at: datetime,
    replace: bool,
) -> FileEvidenceSnapshot:
    """Insert (start: keep the first; end: replace) the snapshot for an attempt."""
    row = get_snapshot(session, attempt_id, kind)
    if row is not None and not replace:
        return row
    if row is None:
        row = FileEvidenceSnapshot(run_id=run_id, attempt_id=attempt_id, kind=kind)
        session.add(row)
    row.taken_at = taken_at
    row.is_repo = bool(data.get("is_repo"))
    row.head = data.get("head")
    row.branch = data.get("branch")
    row.dirty = data.get("dirty")
    row.files = dict(data.get("files") or {})
    details = dict(row.details or {})
    details.update(data.get("details") or {})
    details["file_count"] = len(row.files)
    row.details = details
    session.flush()
    return row


def update_observer(session: Session, attempt_id: str, observer: dict[str, Any]) -> None:
    row = get_snapshot(session, attempt_id, "start")
    if row is None:
        return
    details = dict(row.details or {})
    windows = list(details.get("observers") or [])
    if windows and windows[-1].get("session") == observer.get("session"):
        windows[-1] = {**windows[-1], **observer}
    else:
        windows.append(observer)
    details["observers"] = windows
    row.details = details
    session.flush()


def as_snapshot(row: FileEvidenceSnapshot | None) -> Snapshot | None:
    if row is None:
        return None
    return Snapshot(
        is_repo=row.is_repo,
        files=dict(row.files or {}),
        head=row.head,
        taken_at=row.taken_at,
        details=dict(row.details or {}),
    )


def run_snapshots(
    session: Session, run_id: str, *, files: bool = False
) -> tuple[FileEvidenceSnapshot | None, FileEvidenceSnapshot | None, list[FileEvidenceSnapshot]]:
    """(run start = earliest start, run end = latest end if newer than every start, starts).

    The per-file state maps are only loaded with ``files=True`` (rebuilds, reconciliation,
    timelines); status and summary requests read the snapshot metadata alone.
    """
    stmt = (
        select(FileEvidenceSnapshot)
        .where(FileEvidenceSnapshot.run_id == run_id)
        .order_by(FileEvidenceSnapshot.taken_at, FileEvidenceSnapshot.id)
    )
    if not files:
        stmt = stmt.options(defer(FileEvidenceSnapshot.files, raiseload=True))
    rows = list(session.scalars(stmt))
    starts = [r for r in rows if r.kind == "start"]
    ends = [r for r in rows if r.kind == "end"]
    start = starts[0] if starts else None
    end = ends[-1] if ends else None
    if end is not None and starts:
        # A newer attempt started after the last reconciliation: the end is stale.
        latest_start = starts[-1]
        attempt_ends = {r.attempt_id for r in ends}
        if latest_start.attempt_id not in attempt_ends and latest_start.taken_at > end.taken_at:
            end = None
    return start, end, starts


# --------------------------------------------------------------------------- rebuild


_LEDGER_COLUMNS = [
    getattr(FileEvidenceEvent, f)
    for f in (
        "sequence",
        "ts",
        "operation",
        "path",
        "previous_path",
        "inside_boundary",
        "source",
        "count",
        "attempt_id",
        "node_key",
        "adapter",
        "tool",
        "command",
        "after_hash",
        "policy",
        "details",
    )
]


def _ledger_events(session: Session, run_id: str) -> list[LedgerEvent]:
    """The run's evidence as plain tuples (no ORM objects; streamed in chunks)."""
    stmt = (
        select(*_LEDGER_COLUMNS)
        .where(FileEvidenceEvent.run_id == run_id)
        .order_by(FileEvidenceEvent.ts, FileEvidenceEvent.sequence)
        .execution_options(yield_per=1000)
    )
    return [
        LedgerEvent(**{**r._asdict(), "details": r.details or {}}) for r in session.execute(stmt)
    ]


def supersede_reconciliation(
    session: Session, run_id: str, attempt_id: str, keep: set[str]
) -> None:
    """Drop an attempt's earlier Git end / reconciliation rows that a newer reconciliation
    no longer reports (a pause for approval reconciles provisionally; the resumed attempt
    may revert that change). Live observations are never touched."""
    stale = select(FileEvidenceEvent.id).where(
        FileEvidenceEvent.run_id == run_id,
        FileEvidenceEvent.attempt_id == attempt_id,
        FileEvidenceEvent.source.in_(("git_end", "reconciliation")),
    )
    if keep:
        stale = stale.where(FileEvidenceEvent.dedupe_key.not_in(sorted(keep)))
    session.execute(delete(FileEvidenceEvent).where(FileEvidenceEvent.id.in_(stale)))


def command_windows(session: Session, run_id: str) -> list[CommandWindow]:
    """Commands that declared their start/finish (demo agent, hooks) for attribution."""
    out: list[CommandWindow] = []
    rows = session.execute(
        select(Event.payload, Event.summary).where(
            Event.run_id == run_id, Event.type == EventType.COMMAND.value
        )
    )
    for payload, summary in rows:
        started = (payload or {}).get("started_at")
        finished = (payload or {}).get("finished_at")
        if not started or not finished:
            continue
        try:
            s, f = datetime.fromisoformat(str(started)), datetime.fromisoformat(str(finished))
        except ValueError:
            continue
        out.append(
            CommandWindow(
                display=str(summary)[:200],
                category=(payload or {}).get("category"),
                started=s,
                finished=f,
            )
        )
    return sorted(out, key=lambda w: w.started)


def offline_windows(session: Session, run_id: str) -> list[OfflineWindow]:
    """Runtime reconnects mark windows in which AgentTape was not observing."""
    out: list[OfflineWindow] = []
    rows = session.scalars(
        select(Event.payload).where(Event.run_id == run_id, Event.type == EventType.RUNTIME.value)
    )
    for payload in rows:
        p = payload or {}
        if p.get("action") == "reconnect":
            out.append(OfflineWindow(p.get("offline_from"), p.get("offline_to")))
    return out


def observer_windows(
    session: Session, starts: Iterable[FileEvidenceSnapshot]
) -> list[ObserverWindow]:
    out: list[ObserverWindow] = []
    for s in starts:
        attempt = session.get(RunAttempt, s.attempt_id)
        finished = attempt is not None and attempt.status in TERMINAL_STATUS_VALUES
        for w in (s.details or {}).get("observers") or []:
            out.append(
                ObserverWindow(
                    attempt_id=s.attempt_id,
                    status=str(w.get("status") or "unavailable"),
                    reason=w.get("reason"),
                    started_at=_dt(w.get("started_at")),
                    stopped_at=_dt(w.get("stopped_at")),
                    attempt_finished=finished,
                    dropped=int(w.get("dropped") or 0),
                )
            )
    return out


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _observed_reads(session: Session, run: Run) -> ObservedReads | None:
    """For a run read only through Claude Code hooks (observed or Herdr-hosted), how its
    reads reached AgentTape, worded from the same coverage
    facts and gaps citation verification uses (``findings.verify.observed_gaps``);
    ``None`` for a run AgentTape launched."""
    coverage = observed_coverage(session, run)
    if coverage is None:
        return None
    return ObservedReads(
        hooks="no_hooks" not in observed_gaps(coverage),
        gap_statement=observed_statement(coverage),
    )


def completeness(session: Session, run: Run, *, reconciling: bool = False) -> dict[str, Any]:
    """The run's completeness statement. ``reconciling``: called by the final reconciliation
    (the run still looks active then, but its end snapshot is current)."""
    start, end, starts = run_snapshots(session, run.id)
    observers = observer_windows(session, starts)
    reads = int(
        session.scalar(
            select(func.count(FileEvidenceEvent.id)).where(
                FileEvidenceEvent.run_id == run.id, FileEvidenceEvent.operation == "read"
            )
        )
        or 0
    )
    waiting = run.status == RunStatus.WAITING_FOR_APPROVAL.value
    active = run.status not in TERMINAL_STATUS_VALUES and not waiting
    if active and not reconciling:
        end = None  # an end snapshot from before a resume is not final
    result = run_completeness(
        adapter=run.adapter_type,
        observers=observers,
        has_start=start is not None,
        has_end=end is not None,
        is_repo=bool(start.is_repo) if start is not None else False,
        read_events=reads,
        run_active=active and end is None,
        offline=offline_windows(session, run.id),
        observed_reads=_observed_reads(session, run),
    )
    if waiting and end is not None:
        result["git"] = "provisional"
        result["level"] = "pending"
        result["statement"] = (
            result["statement"]
            .replace("Writes and final changes fully observed", "Writes observed so far")
            .replace(
                "final Git reconciliation completed",
                "Git reconciliation is provisional while the run waits for approval",
            )
        )
        if "provisional" not in result["statement"]:
            result["statement"] = result["statement"].replace(
                "Writes observed so far",
                "Writes observed so far; Git reconciliation is provisional while the run "
                "waits for approval",
                1,
            )
    result["step_attribution"] = step_attribution(session, run)
    result["start"] = _snapshot_meta(start)
    result["end"] = _snapshot_meta(end)
    return result


def step_attribution(session: Session, run: Run) -> str:
    """single_step | unavailable | not_a_workflow_step (never guessed)."""
    if not run.parent_node_key:
        return "not_a_workflow_step"
    from agenttape.infrastructure.db.extra_models.git_workspaces import RunWorkspace

    ws = session.scalars(select(RunWorkspace).where(RunWorkspace.run_id == run.id)).first()
    return "single_step" if ws is not None and ws.isolated else "unavailable"


def _snapshot_meta(row: FileEvidenceSnapshot | None) -> dict[str, Any] | None:
    if row is None:
        return None
    details = row.details or {}
    return {
        "attempt_id": row.attempt_id,
        "taken_at": row.taken_at.isoformat(),
        "is_repo": row.is_repo,
        "head": row.head,
        "branch": row.branch,
        "dirty": row.dirty,
        "file_count": details.get("file_count"),
        "truncated": bool(details.get("truncated")),
        "noise_policy": details.get("noise_policy"),
    }


def rebuild_summaries(
    session: Session, run_id: str, *, now: datetime, live_attempt: str | None = None
) -> list[RunFileSummary]:
    """Replace the run's summaries with a deterministic rebuild from evidence + snapshots.

    ``live_attempt``: the attempt currently executing (live rebuilds by the monitor); an
    end snapshot it took earlier (a pause for approval) is stale, so states are pending.
    """
    run = session.get(Run, run_id)
    if run is None:
        return []
    start_row, end_row, starts = run_snapshots(session, run_id, files=True)
    if end_row is not None and live_attempt is not None and end_row.attempt_id == live_attempt:
        end_row = None
    observers = observer_windows(session, starts)
    summaries = build_summaries(
        _ledger_events(session, run_id),
        as_snapshot(start_row),
        as_snapshot(end_row),
        patterns=protected_patterns(run),
        commands=command_windows(session, run_id),
        watcher_active=any(o.status != "unavailable" for o in observers),
    )
    session.execute(delete(RunFileSummary).where(RunFileSummary.run_id == run_id))
    rows = [_summary_row(run_id, s, now) for s in summaries]
    session.add_all(rows)
    session.flush()
    return rows


def _summary_row(run_id: str, s: Summary, now: datetime) -> RunFileSummary:
    return RunFileSummary(
        run_id=run_id,
        file_key=s.file_key,
        start_path=s.start_path,
        end_path=s.end_path,
        rename_chain=s.rename_chain,
        first_at=s.first_at,
        last_at=s.last_at,
        operation_counts=s.operation_counts,
        operation_total=s.operation_total,
        first_step=s.first_step,
        last_step=s.last_step,
        attempts=s.attempts,
        agents=s.agents,
        tools=s.tools,
        sources=s.sources,
        classification=s.classification,
        classifications=s.classifications,
        start_hash=s.start_hash,
        end_hash=s.end_hash,
        size_bytes=s.size_bytes,
        is_binary=s.is_binary,
        net_status=s.net_status,
        insertions=s.insertions,
        deletions=s.deletions,
        net_unchanged=s.net_unchanged,
        is_protected=s.is_protected,
        protected_patterns=s.protected_patterns,
        is_generated=s.is_generated,
        is_outside=s.is_outside,
        is_collapsed=s.is_collapsed,
        collapsed_count=s.collapsed_count,
        completeness=s.completeness,
        explanation=s.explanation,
        policy=s.policy,
    )
