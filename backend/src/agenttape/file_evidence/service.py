"""Application service for File Evidence: ledger queries, timelines, exports, aggregates.

Shared by the API router and the CLI-free tests. Filtering, sorting, pagination, and
aggregation run in SQL, so a request never loads a whole ledger (a file's evidence trail
is capped at ``MAX_TIMELINE_EVENTS``, project aggregates cover the last
``MAX_PROJECT_RUNS`` runs), and every export passes through the export redactor with
repository-relative paths only.
"""

from __future__ import annotations

import csv
import io
import itertools
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import ColumnElement, Text, and_, case, cast, func, or_, select
from sqlalchemy.orm import Session

from agenttape import __version__
from agenttape.application.context import AppContext
from agenttape.application.schemas.common import Page
from agenttape.application.services.audit import record_audit
from agenttape.application.services.common import get_or_404, like_pattern
from agenttape.context_evidence.export import context_export
from agenttape.domain.enums import TERMINAL_STATUS_VALUES, RunStatus
from agenttape.domain.errors import ConflictError, NotFoundError, ValidationError
from agenttape.file_evidence import store
from agenttape.file_evidence.ledger import LABELS, OPERATIONS, WRITE_OPS, NoisePolicy
from agenttape.file_evidence.schemas import (
    ActivityEntry,
    ChangedTogether,
    CollapsedDir,
    Completeness,
    EvidenceEventOut,
    FileCounts,
    FileDiff,
    FileRunHistoryItem,
    FileState,
    FileTimeline,
    Finding,
    LinkedApproval,
    LinkedEvent,
    ProjectFileActivity,
    RebuildResult,
    RunFileEvidenceSummary,
    RunFileOut,
    RunFileRef,
)
from agenttape.infrastructure.db.extra_models.file_evidence import (
    FileEvidenceEvent,
    RunFileSummary,
)
from agenttape.infrastructure.db.extra_models.runtime_sessions import RuntimeSession
from agenttape.infrastructure.db.models import ApprovalRequest, Event, Project, Run, RunAttempt

MAX_TIMELINE_EVENTS = 500
MAX_EXPORT_EVENTS = 20000
MAX_PROJECT_RUNS = 200
MAX_CHANGED_TOGETHER_PATHS = 50
FAILED_STATUSES = {RunStatus.FAILED.value, RunStatus.TIMED_OUT.value}
READ_CLASSES = ("observed_read", "inferred_access")
READ_OPS = ("read", "search", "reference")
CHANGED_NET = ("added", "modified", "deleted", "renamed")
RFS = RunFileSummary
#: Runtime reference columns safe to export (never sockets, native session values, paths).
RUNTIME_COLUMNS = (
    "attempt_id",
    "runtime",
    "machine",
    "workspace_id",
    "tab_id",
    "pane_id",
    "agent_name",
    "agent_kind",
    "lifecycle_state",
    "herdr_version",
    "protocol",
    "status",
)
LEDGER_COLUMNS = [
    "path",
    "start_path",
    "rename_chain",
    "classification",
    "label",
    "net_status",
    "net_unchanged",
    "operations",
    "operation_total",
    "first_at",
    "last_at",
    "first_step",
    "last_step",
    "agents",
    "tools",
    "sources",
    "protected",
    "protected_patterns",
    "outside_boundary",
    "generated",
    "collapsed_count",
    "insertions",
    "deletions",
    "start_hash",
    "end_hash",
    "completeness",
    "explanation",
]
EVENT_COLUMNS = [
    "sequence",
    "ts",
    "operation",
    "path",
    "previous_path",
    "inside_boundary",
    "source",
    "quality",
    "attempt_id",
    "node_key",
    "adapter",
    "runtime",
    "tool",
    "command",
    "count",
    "before_hash",
    "after_hash",
    "size_bytes",
    "is_binary",
    "protected_patterns",
    "policy_decision",
    "redacted",
]


@dataclass
class FileFilters:
    classification: list[str] | None = None
    operation: list[str] | None = None
    net_status: list[str] | None = None
    source: list[str] | None = None
    step: str | None = None
    agent: str | None = None
    protected: bool | None = None
    outside: bool | None = None
    generated: bool | None = None
    net_unchanged: bool | None = None
    q: str | None = None
    sort: str = "path"
    order: str = "asc"

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, [], "")}


def _out(row: RunFileSummary) -> RunFileOut:
    out = RunFileOut.model_validate(row)
    out.label = (
        "Collapsed noisy activity"
        if row.is_collapsed
        else LABELS.get(row.classification, row.classification)
    )
    return out


def _mentions(column: Any, value: str) -> ColumnElement[bool]:
    """A JSON list (or object) column contains the string ``value`` (as an item or key).

    Matches the serialized JSON text, which SQLite and PostgreSQL both produce with the
    value quoted; both ASCII-escaped and raw spellings are tried.
    """
    tokens = {json.dumps(value), json.dumps(value, ensure_ascii=False)}
    text = cast(column, Text)
    return or_(*(text.like("%" + _escape(t) + "%", escape="\\") for t in sorted(tokens)))


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _conditions(run_id: str, f: FileFilters) -> list[ColumnElement[bool]]:
    conds: list[ColumnElement[bool]] = [RFS.run_id == run_id]
    if f.classification:
        conds.append(RFS.classification.in_(f.classification))
    if f.operation:
        conds.append(or_(*(_mentions(RFS.operation_counts, op) for op in f.operation)))
    if f.net_status:
        conds.append(RFS.net_status.in_(f.net_status))
    if f.source:
        conds.append(or_(*(_mentions(RFS.sources, s) for s in f.source)))
    if f.step:
        conds.append(or_(RFS.first_step == f.step, RFS.last_step == f.step))
    if f.agent:
        conds.append(_mentions(RFS.agents, f.agent))
    for flag, column in (
        (f.protected, RFS.is_protected),
        (f.outside, RFS.is_outside),
        (f.generated, RFS.is_generated),
        (f.net_unchanged, RFS.net_unchanged),
    ):
        if flag is not None:
            conds.append(column.is_(flag))
    if f.q:
        pattern = like_pattern(f.q)
        conds.append(
            or_(
                *(
                    func.lower(c).like(pattern, escape="\\")
                    for c in (RFS.file_key, RFS.start_path, cast(RFS.rename_chain, Text))
                )
            )
        )
    return conds


def _order(sort: str, order: str) -> list[Any]:
    options: dict[str, list[Any]] = {
        "first_activity": [RFS.first_at.is_(None), RFS.first_at],
        "last_activity": [RFS.last_at.is_(None), RFS.last_at],
        "operations": [RFS.operation_total],
        "diff_size": [func.coalesce(RFS.insertions, 0) + func.coalesce(RFS.deletions, 0)],
    }
    keys = [*options.get(sort, [RFS.is_collapsed]), RFS.file_key]
    return [k.desc() for k in keys] if order == "desc" else keys


def _count_if(cond: ColumnElement[bool]) -> Any:
    return func.coalesce(func.sum(case((cond, 1), else_=0)), 0)


def _safe_path(path: str | None, inside: bool = True) -> str | None:
    """Repository-relative paths only: outside-boundary paths keep just their name."""
    if path is None:
        return None
    if not inside or path.startswith(("/", "~")) or path.startswith(".."):
        return f"<outside-boundary>/{PurePosixPath(path).name}"
    return path


class FileEvidenceService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------------ ledger

    def _select(
        self, session: Session, run_id: str, filters: FileFilters
    ) -> Any:  # a Select[tuple[RunFileSummary]]
        get_or_404(session, Run, run_id, "run")
        return select(RFS).where(*_conditions(run_id, filters))

    def list_files(
        self, run_id: str, filters: FileFilters, *, limit: int, offset: int
    ) -> Page[RunFileOut]:
        with self.ctx.session() as session:
            stmt = self._select(session, run_id, filters)
            total = int(session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
            rows = session.scalars(
                stmt.order_by(*_order(filters.sort, filters.order)).limit(limit).offset(offset)
            ).all()
            items = [_out(r) for r in rows]
        return Page[RunFileOut](
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            next_offset=offset + limit if offset + limit < total else None,
        )

    def summary(self, run_id: str) -> RunFileEvidenceSummary:
        files_only = RFS.is_collapsed.is_(False)
        counted = {
            "files": _count_if(files_only),
            "reads": _count_if(
                and_(
                    files_only,
                    or_(*(_mentions(RFS.operation_counts, op) for op in READ_OPS)),
                )
            ),
            "created": _count_if(and_(files_only, RFS.classification == "created")),
            "modified": _count_if(and_(files_only, RFS.classification == "modified")),
            "deleted": _count_if(and_(files_only, RFS.classification == "deleted")),
            "renamed": _count_if(
                and_(
                    files_only,
                    or_(RFS.classification == "renamed", _mentions(RFS.operation_counts, "rename")),
                )
            ),
            "net_unchanged": _count_if(and_(files_only, RFS.net_unchanged.is_(True))),
            "generated": _count_if(and_(files_only, RFS.is_generated.is_(True))),
            **{
                f"op_{op}": _count_if(and_(files_only, _mentions(RFS.operation_counts, op)))
                for op in OPERATIONS
                if op != "collapsed"
            },
        }
        with self.ctx.session() as session:
            run = get_or_404(session, Run, run_id, "run")
            status = store.completeness(session, run)
            run_status = run.status
            in_run = RFS.run_id == run_id
            agg = session.execute(select(*counted.values()).where(in_run)).one()
            n = dict(zip(counted, (int(v or 0) for v in agg), strict=True))
            collapsed = session.scalars(
                select(RFS)
                .where(in_run, RFS.is_collapsed.is_(True))
                .order_by(RFS.collapsed_count.desc(), RFS.file_key)
            ).all()
            protected = session.scalars(
                select(RFS)
                .where(
                    in_run,
                    files_only,
                    RFS.is_protected.is_(True),
                    or_(
                        RFS.net_status.in_(CHANGED_NET),
                        *(_mentions(RFS.operation_counts, op) for op in sorted(WRITE_OPS)),
                    ),
                )
                .order_by(RFS.file_key)
            ).all()
            outside = session.scalars(
                select(RFS)
                .where(in_run, files_only, RFS.is_outside.is_(True))
                .order_by(RFS.file_key)
            ).all()

            def distinct(column: Any, *where: Any) -> list[Any]:
                return list(session.scalars(select(column).where(in_run, *where).distinct()))

            facets = {
                "classification": sorted(distinct(RFS.classification, files_only)),
                "net_status": sorted(distinct(RFS.net_status, files_only)),
                "source": sorted({s for v in distinct(RFS.sources) for s in v or []}),
                "operation": [op for op in OPERATIONS if n.get(f"op_{op}")],
                "step": sorted(
                    {s for c in (RFS.first_step, RFS.last_step) for s in distinct(c) if s}
                ),
                "agent": sorted({a for v in distinct(RFS.agents, files_only) for a in v or []}),
            }
            collapsed_dirs = [
                CollapsedDir(
                    path=r.file_key,
                    count=r.collapsed_count or 0,
                    by_operation={k: int(v) for k, v in (r.operation_counts or {}).items()},
                )
                for r in collapsed
            ]
            protected_findings, outside_findings = (
                [
                    Finding(
                        file_key=r.file_key,
                        classification=r.classification,
                        net_status=r.net_status,
                        patterns=list(r.protected_patterns or []),
                        policy=r.policy,
                    )
                    for r in rows
                ]
                for rows in (protected, outside)
            )
        unavailable: dict[str, str] = {}
        writes_known = status["writes"] != "unavailable" or status["git"] in (
            "completed",
            "filesystem_only",
        )

        def count(key: str) -> int | None:
            return n[key] if writes_known else None

        reads: int | None = n["reads"]
        if status["reads"] == "unavailable" and not reads:
            reads = None
            unavailable["read_or_referenced"] = (
                "This agent's telemetry does not report file reads; reads were not captured."
            )
        if not writes_known:
            for key in (
                "created",
                "modified",
                "deleted",
                "renamed",
                "restored_or_net_unchanged",
                "generated",
            ):
                unavailable[key] = (
                    "Neither filesystem observation nor a final Git reconciliation is available."
                )
        return RunFileEvidenceSummary(
            run_id=run_id,
            run_status=run_status,
            completeness=Completeness.model_validate(status),
            counts=FileCounts(
                files=n["files"],
                read_or_referenced=reads,
                created=count("created"),
                modified=count("modified"),
                deleted=count("deleted"),
                renamed=count("renamed"),
                restored_or_net_unchanged=count("net_unchanged"),
                generated=count("generated"),
                protected_findings=len(protected_findings),
                outside_findings=len(outside_findings),
                collapsed_events=sum(c.count for c in collapsed_dirs),
                collapsed_directories=len(collapsed_dirs),
            ),
            unavailable=unavailable,
            collapsed=collapsed_dirs,
            protected=protected_findings,
            outside=outside_findings,
            facets=facets,
        )

    # ------------------------------------------------------------------ timeline

    def timeline(self, run_id: str, file_key: str) -> FileTimeline:
        with self.ctx.session() as session:
            get_or_404(session, Run, run_id, "run")
            row = session.scalars(
                select(RunFileSummary).where(
                    RunFileSummary.run_id == run_id, RunFileSummary.file_key == file_key
                )
            ).first()
            if row is None:
                raise NotFoundError("file", file_key)
            paths = sorted(
                {
                    row.file_key,
                    *(row.rename_chain or []),
                    *([row.start_path] if row.start_path else []),
                }
            )
            stmt = (
                select(FileEvidenceEvent)
                .where(FileEvidenceEvent.run_id == run_id)
                .where(
                    FileEvidenceEvent.path.in_(paths) | FileEvidenceEvent.previous_path.in_(paths)
                )
                .order_by(FileEvidenceEvent.ts, FileEvidenceEvent.sequence)
                .limit(MAX_TIMELINE_EVENTS + 1)
            )
            events = list(session.scalars(stmt))
            truncated = len(events) > MAX_TIMELINE_EVENTS
            events = events[:MAX_TIMELINE_EVENTS]
            source_ids = sorted({e.source_event_id for e in events if e.source_event_id})
            linked = [
                LinkedEvent(id=e.id, sequence=e.sequence, type=e.type, summary=e.summary, ts=e.ts)
                for e in session.scalars(
                    select(Event).where(Event.id.in_(source_ids)).order_by(Event.sequence)
                )
            ]
            approval_ids = {str((e.policy or {}).get("approval_id")) for e in events}
            approvals = []
            for a in session.scalars(
                select(ApprovalRequest).where(ApprovalRequest.run_id == run_id)
            ):
                action = a.requested_action or {}
                declared = set(action.get("paths") or []) | set(action.get("files") or [])
                if a.id in approval_ids or declared & set(paths):
                    approvals.append(
                        LinkedApproval(
                            id=a.id,
                            title=a.title,
                            status=a.status,
                            type=a.type,
                            reviewer=a.reviewer,
                            decided_at=a.resolved_at,
                        )
                    )
            start_row, end_row, _starts = store.run_snapshots(session, run_id, files=True)
            start_snap = store.as_snapshot(start_row)
            end_snap = store.as_snapshot(end_row)
            out_events = [EvidenceEventOut.model_validate(e) for e in events]
            file_out = _out(row)
        start_path = row.start_path or (row.rename_chain[0] if row.rename_chain else row.file_key)

        def state(snap: Any, path: str | None) -> FileState:
            if snap is None or path is None:
                return FileState(path=path, hash=None, size_bytes=None, is_binary=None, exists=None)
            st = snap.state(path)
            known = snap.knows(path) or snap.absent_known(path)
            if st is None:
                return FileState(
                    path=path,
                    hash=None,
                    size_bytes=None,
                    is_binary=None,
                    exists=False if known else None,
                )
            return FileState(
                path=path, hash=st.hash, size_bytes=st.size, is_binary=st.binary, exists=True
            )

        diff: FileDiff | None = None
        try:
            from agenttape.application.services.git import GitService

            view = GitService(self.ctx).view(run_id)
            for f in view.files:
                if f.path in paths or (f.old_path and f.old_path in paths):
                    diff = FileDiff(
                        path=f.path,
                        change_type=f.change_type,
                        insertions=f.insertions,
                        deletions=f.deletions,
                        is_binary=f.is_binary,
                        hunks=[h.model_dump() for h in f.hunks],
                        diff_available=f.diff_available,
                        diff_truncated=f.diff_truncated,
                        diff_omitted=f.diff_omitted,
                    )
                    break
        except Exception:  # the diff is supplementary; the ledger stands without it
            diff = None
        return FileTimeline(
            file=file_out,
            events=out_events,
            truncated=truncated,
            start_state=state(start_snap, start_path),
            end_state=state(end_snap, row.file_key)
            if end_snap is not None
            else FileState(
                path=row.file_key, hash=None, size_bytes=None, is_binary=None, exists=None
            ),
            diff=diff,
            linked_events=linked,
            approvals=approvals,
        )

    # ------------------------------------------------------------------ rebuild

    def rebuild(self, run_id: str, *, actor: str, reconcile: bool = False) -> RebuildResult:
        reconciled = False
        with self.ctx.session() as session:
            run = get_or_404(session, Run, run_id, "run")
            if run.status not in TERMINAL_STATUS_VALUES:
                # Queued, running, or waiting for approval: the worker owns its evidence
                # (a resumed attempt appends and reconciles concurrently otherwise).
                raise ConflictError(
                    "The run can still execute or resume; its evidence is collected by the "
                    "worker. Rebuild once it has finished.",
                    details={"status": run.status},
                )
            attempt = session.scalars(
                select(RunAttempt)
                .where(RunAttempt.run_id == run_id)
                .order_by(RunAttempt.attempt_number.desc())
            ).first()
            workdir = attempt.workdir if attempt is not None else None
            attempt_id = attempt.id if attempt is not None else None
            settings = store.project_settings(run.snapshot or {})
            has_start = store.run_snapshots(session, run_id)[0] is not None
        if reconcile:
            if not (workdir and attempt_id and has_start and Path(workdir).is_dir()):
                raise ConflictError(
                    "Reconciliation needs the run's workdir and a start snapshot; "
                    "the summaries were not changed.",
                    details={"workdir_exists": bool(workdir and Path(workdir).is_dir())},
                )
            from agenttape.file_evidence.collector import reconcile_run

            reconcile_run(
                self.ctx,
                run_id=run_id,
                attempt_id=attempt_id,
                root=Path(workdir).resolve(),
                noise=NoisePolicy.from_settings(settings),
            )
            reconciled = True
        with self.ctx.session() as session:
            rows = store.rebuild_summaries(session, run_id, now=self.ctx.clock.now())
            record_audit(
                session,
                self.ctx.clock,
                action="file_evidence.rebuild",
                resource_type="run",
                resource_id=run_id,
                actor=actor,
                run_id=run_id,
                details={"reconciled": reconciled, "files": len(rows)},
            )
            files = sum(1 for r in rows if not r.is_collapsed)
        return RebuildResult(
            run_id=run_id, reconciled=reconciled, files=files, summary=self.summary(run_id)
        )

    # ------------------------------------------------------------------ exports

    def _redactor(self) -> Any:
        from agenttape.application.services.exports import ExportService

        return ExportService(self.ctx).export_redactor()

    def _ledger_record(self, r: RunFileSummary) -> dict[str, Any]:
        inside = not r.is_outside
        return {
            "path": _safe_path(r.file_key, inside),
            "start_path": _safe_path(r.start_path, inside),
            "rename_chain": [_safe_path(p, inside) for p in r.rename_chain or []],
            "classification": "collapsed" if r.is_collapsed else r.classification,
            "label": "Collapsed noisy activity"
            if r.is_collapsed
            else LABELS.get(r.classification, r.classification),
            "net_status": r.net_status,
            "net_unchanged": r.net_unchanged,
            "operations": dict(r.operation_counts or {}),
            "operation_total": r.operation_total,
            "first_at": r.first_at.isoformat() if r.first_at else None,
            "last_at": r.last_at.isoformat() if r.last_at else None,
            "first_step": r.first_step,
            "last_step": r.last_step,
            "agents": list(r.agents or []),
            "tools": list(r.tools or []),
            "sources": list(r.sources or []),
            "protected": r.is_protected,
            "protected_patterns": list(r.protected_patterns or []),
            "outside_boundary": r.is_outside,
            "generated": r.is_generated,
            "collapsed_count": r.collapsed_count,
            "insertions": r.insertions,
            "deletions": r.deletions,
            "start_hash": r.start_hash,
            "end_hash": r.end_hash,
            "completeness": r.completeness,
            "explanation": r.explanation if inside else "Outside the permitted project root.",
            "policy": r.policy,
        }

    def export_ledger(self, run_id: str, filters: FileFilters, fmt: str) -> tuple[bytes, str, str]:
        if fmt not in ("csv", "json"):
            raise ValidationError("format must be csv or json")
        redactor = self._redactor()
        with self.ctx.session() as session:
            stmt = self._select(session, run_id, filters)
            rows = session.execute(
                stmt.order_by(*_order(filters.sort, filters.order)).execution_options(yield_per=500)
            ).scalars()
            records = [redactor.redact_value(self._ledger_record(r)) for r in rows]
            run = session.get(Run, run_id)
            status = store.completeness(session, run) if run is not None else {}
        name = f"agenttape-run-{run_id}-files.{fmt}"
        if fmt == "json":
            doc = {
                "format": "agenttape.file-ledger",
                "format_version": 1,
                "exported_at": self.ctx.clock.now().isoformat(),
                "run_id": run_id,
                "filters": filters.as_dict(),
                "completeness": {
                    k: status[k] for k in ("level", "statement", "writes", "reads", "git")
                },
                "files": records,
            }
            return (
                (json.dumps(doc, indent=2, default=str) + "\n").encode(),
                "application/json",
                name,
            )
        return _csv(records, LEDGER_COLUMNS), "text/csv", name

    def evidence_bundle(self, run_id: str, fmt: str) -> tuple[bytes, str, str]:
        """The full sanitized evidence bundle (JSON) or its evidence events (CSV)."""
        if fmt not in ("csv", "json"):
            raise ValidationError("format must be csv or json")
        redactor = self._redactor()
        with self.ctx.session() as session:
            run = get_or_404(session, Run, run_id, "run")
            status = store.completeness(session, run)
            rows = list(
                session.scalars(
                    select(RFS).where(RFS.run_id == run_id).order_by(RFS.is_collapsed, RFS.file_key)
                )
            )
            events = list(
                session.scalars(
                    select(FileEvidenceEvent)
                    .where(FileEvidenceEvent.run_id == run_id)
                    .order_by(FileEvidenceEvent.ts, FileEvidenceEvent.sequence)
                    .limit(MAX_EXPORT_EVENTS + 1)
                )
            )
            start_row, end_row, _ = store.run_snapshots(session, run_id)
            event_records = [
                redactor.redact_value(_event_record(e)) for e in events[:MAX_EXPORT_EVENTS]
            ]
            ledger = [redactor.redact_value(self._ledger_record(r)) for r in rows]
            runtime = _runtime_refs(session, run_id)
            context = _context_export(session, run_id)
            run_meta = {
                "id": run.id,
                "title": redactor.redact_text(run.title),
                "status": run.status,
                "adapter_type": run.adapter_type,
                "created_at": run.created_at.isoformat(),
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            }
            git = {
                "start": _git_meta(start_row, include_numstat=False),
                "end": _git_meta(end_row, include_numstat=True),
            }
        if fmt == "csv":
            return (
                _csv(event_records, EVENT_COLUMNS),
                "text/csv",
                f"agenttape-run-{run_id}-evidence.csv",
            )
        bundle = {
            "format": "agenttape.evidence-bundle",
            "format_version": 1,
            "exported_at": self.ctx.clock.now().isoformat(),
            "agenttape_version": __version__,
            "run": run_meta,
            "completeness": {
                k: status[k]
                for k in (
                    "level",
                    "statement",
                    "writes",
                    "reads",
                    "git",
                    "step_attribution",
                    "offline_windows",
                    "dropped_watcher_events",
                )
            },
            "git": git,
            "final_diff": (git["end"] or {}).get("numstat", []),
            "file_summaries": [r for r in ledger if r["classification"] != "collapsed"],
            "collapsed_directories": [
                {"path": r["path"], "count": r["collapsed_count"], "operations": r["operations"]}
                for r in ledger
                if r["classification"] == "collapsed"
            ],
            "findings": {
                "protected": [
                    r
                    for r in ledger
                    if r["protected"]
                    and r["classification"] not in ("observed_read", "inferred_access", "collapsed")
                ],
                "outside_boundary": [r for r in ledger if r["outside_boundary"]],
            },
            "evidence_events": event_records,
            "evidence_events_truncated": len(events) > MAX_EXPORT_EVENTS,
            "runtime": runtime,
            "context": context,
            "privacy": {
                "paths": "repository-relative; outside-boundary paths reduced to their file name",
                "contents": "no file contents are stored or exported; hashes are Git blob ids",
                "redaction": "credential patterns and known secret values redacted at export time",
            },
        }
        body = json.dumps(redactor.redact_value(bundle), indent=2, default=str) + "\n"
        return body.encode(), "application/json", f"agenttape-run-{run_id}-evidence.json"

    # ------------------------------------------------------------------ project

    def _recent_runs(self, session: Session, project_id: str) -> dict[str, Any]:
        """The project's last ``MAX_PROJECT_RUNS`` runs (id, title, status, created_at)."""
        get_or_404(session, Project, project_id, "project")
        rows = session.execute(
            select(Run.id, Run.title, Run.status, Run.created_at)
            .where(Run.project_id == project_id)
            .order_by(Run.created_at.desc())
            .limit(MAX_PROJECT_RUNS)
        ).all()
        return {r.id: r for r in rows}

    def project_activity(self, project_id: str) -> ProjectFileActivity:
        with self.ctx.session() as session:
            runs = self._recent_runs(session, project_id)
            scope = [RFS.run_id.in_(list(runs)), RFS.is_collapsed.is_(False)]
            inside = [*scope, RFS.is_outside.is_(False)]
            failed_ids = [i for i, r in runs.items() if r.status in FAILED_STATUSES]

            def ranked(where: list[Any], *, by_operations: bool) -> list[ActivityEntry]:
                n_runs = func.count(func.distinct(RFS.run_id))
                ops = func.coalesce(func.sum(RFS.operation_total), 0)
                grouped = session.execute(
                    select(RFS.file_key, n_runs, ops, func.max(RFS.last_at))
                    .where(*where)
                    .group_by(RFS.file_key)
                    .order_by(n_runs.desc(), *([ops.desc()] if by_operations else []), RFS.file_key)
                    .limit(20)
                ).all()
                recent: dict[str, list[str]] = defaultdict(list)
                keys = [g[0] for g in grouped]
                for key, run_id in session.execute(
                    select(RFS.file_key, RFS.run_id).where(*where, RFS.file_key.in_(keys))
                ):
                    recent[key].append(run_id)
                return [
                    ActivityEntry(
                        path=key,
                        runs=int(n),
                        operations=int(total),
                        last_at=last,
                        run_ids=sorted(
                            set(recent[key]), key=lambda i: runs[i].created_at, reverse=True
                        )[:10],
                    )
                    for key, n, total, last in grouped
                ]

            touched = [*inside, RFS.classification.not_in(READ_CLASSES)]
            most = ranked(touched, by_operations=True)
            failed = ranked([*inside, RFS.run_id.in_(failed_ids)], by_operations=False)

            # Pairs of files with a net change in the same run (first 50 paths per run).
            rank = func.row_number().over(partition_by=RFS.run_id, order_by=RFS.file_key)
            changed = (
                select(RFS.run_id, RFS.file_key, rank.label("rank"))
                .where(*scope, RFS.net_status.in_(CHANGED_NET))
                .subquery()
            )
            changed_by_run: dict[str, list[str]] = defaultdict(list)
            for run_id, key in session.execute(
                select(changed.c.run_id, changed.c.file_key).where(
                    changed.c.rank <= MAX_CHANGED_TOGETHER_PATHS
                )
            ):
                changed_by_run[run_id].append(key)

            def refs(where: list[Any]) -> list[RunFileRef]:
                rows = session.scalars(
                    select(RFS)
                    .where(*where)
                    .order_by(RFS.last_at.is_(None), RFS.last_at.desc(), RFS.file_key)
                    .limit(30)
                ).all()
                return [
                    RunFileRef(
                        path=r.file_key,
                        run_id=r.run_id,
                        run_title=runs[r.run_id].title,
                        run_status=runs[r.run_id].status,
                        classification=r.classification,
                        net_status=r.net_status,
                        last_at=r.last_at,
                        policy=r.policy,
                    )
                    for r in rows
                ]

            protected = refs(
                [*scope, RFS.is_protected.is_(True), RFS.classification.not_in(READ_CLASSES)]
            )
            recent = refs([*scope, RFS.last_at.is_not(None)])
            with_evidence = session.scalar(
                select(func.count(func.distinct(RFS.run_id))).where(*scope)
            )
        pairs: Counter[tuple[str, str]] = Counter()
        pair_runs: dict[tuple[str, str], list[str]] = defaultdict(list)
        for run_id, paths in changed_by_run.items():
            for a, b in itertools.combinations(sorted(set(paths)), 2):
                pairs[(a, b)] += 1
                pair_runs[(a, b)].append(run_id)
        together = [
            ChangedTogether(paths=[a, b], runs=n, run_ids=pair_runs[(a, b)][:10])
            for (a, b), n in sorted(pairs.items(), key=lambda kv: (-kv[1], kv[0]))
            if n >= 2
        ][:20]
        return ProjectFileActivity(
            project_id=project_id,
            runs_scanned=len(runs),
            runs_with_evidence=int(with_evidence or 0),
            most_touched=most,
            failed_run_files=failed,
            changed_together=together,
            protected_activity=protected,
            recent=recent,
        )

    def file_history(
        self, project_id: str, path: str, *, limit: int, offset: int
    ) -> Page[FileRunHistoryItem]:
        with self.ctx.session() as session:
            runs = self._recent_runs(session, project_id)
            stmt = (
                select(RFS, Run.title, Run.status)
                .join(Run, Run.id == RFS.run_id)
                .where(
                    RFS.run_id.in_(list(runs)),
                    RFS.is_collapsed.is_(False),
                    or_(RFS.file_key == path, _mentions(RFS.rename_chain, path)),
                )
            )
            total = int(session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
            page = session.execute(
                stmt.order_by(Run.created_at.desc(), Run.id).limit(limit).offset(offset)
            ).all()
            items = [
                FileRunHistoryItem(
                    run_id=r.run_id,
                    run_title=title,
                    run_status=status,
                    classification=r.classification,
                    net_status=r.net_status,
                    net_unchanged=r.net_unchanged,
                    operation_total=r.operation_total,
                    first_at=r.first_at,
                    last_at=r.last_at,
                    is_protected=r.is_protected,
                    insertions=r.insertions,
                    deletions=r.deletions,
                )
                for r, title, status in page
            ]
        return Page[FileRunHistoryItem](
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            next_offset=offset + limit if offset + limit < total else None,
        )


# --------------------------------------------------------------------------- helpers


#: Leading characters a spreadsheet treats as a formula (CSV/formula injection).
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_cell(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        value = json.dumps(value, sort_keys=True)
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        # Paths, commands, and tool names are agent-controlled; a leading quote makes a
        # spreadsheet show the text instead of evaluating it.
        return "'" + value
    return value


def _csv(records: list[dict[str, Any]], columns: list[str]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for rec in records:
        writer.writerow({k: _csv_cell(v) for k, v in rec.items()})
    return buf.getvalue().encode()


def _event_record(e: FileEvidenceEvent) -> dict[str, Any]:
    inside = e.inside_boundary
    return {
        "sequence": e.sequence,
        "ts": e.ts.isoformat(),
        "last_ts": e.last_ts.isoformat() if e.last_ts else None,
        "operation": e.operation,
        "path": _safe_path(e.path, inside),
        "previous_path": _safe_path(e.previous_path, inside),
        "inside_boundary": inside,
        "source": e.source,
        "quality": e.quality,
        "attempt_id": e.attempt_id,
        "node_key": e.node_key,
        "adapter": e.adapter,
        "runtime": e.runtime,
        "tool": e.tool,
        "command": e.command,
        "count": e.count,
        "before_hash": e.before_hash,
        "after_hash": e.after_hash,
        "size_bytes": e.size_bytes,
        "is_binary": e.is_binary,
        "protected_patterns": list(e.protected_patterns or []),
        "policy_decision": (e.policy or {}).get("decision"),
        "policy": e.policy,
        "redacted": e.redacted,
        "details": {k: v for k, v in (e.details or {}).items() if k != "via_temp"},
    }


def _git_meta(row: Any, *, include_numstat: bool) -> dict[str, Any] | None:
    if row is None:
        return None
    details = row.details or {}
    out: dict[str, Any] = {
        "taken_at": row.taken_at.isoformat(),
        "is_repo": row.is_repo,
        "head": row.head,
        "branch": row.branch,
        "dirty": row.dirty,
        "files_recorded": details.get("file_count"),
        "truncated": bool(details.get("truncated")),
    }
    if include_numstat:
        out["numstat"] = [
            {"path": p, "insertions": v[0], "deletions": v[1], "binary": v[2]}
            for p, v in sorted((details.get("numstat") or {}).items())
        ]
        out["ignored"] = list(details.get("ignored") or [])
    return out


def _runtime_refs(session: Session, run_id: str) -> dict[str, Any]:
    """The run's runtime references (``runtime_sessions``): public columns only."""
    columns = [getattr(RuntimeSession, c) for c in RUNTIME_COLUMNS]
    rows = session.execute(
        select(*columns).where(RuntimeSession.run_id == run_id).order_by(RuntimeSession.created_at)
    ).all()
    if not rows:
        return {"available": False, "reason": "no runtime references recorded"}
    return {
        "available": True,
        "sessions": [dict(zip(RUNTIME_COLUMNS, r, strict=True)) for r in rows],
    }


def _context_export(session: Session, run_id: str) -> dict[str, Any]:
    return {"available": True, **dict(context_export(session, run_id))}
