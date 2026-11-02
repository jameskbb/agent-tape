"""``FindingsService``: the one read/write path for findings, citations, and citation
verdicts.

A FINDING event's ``links`` may carry a legacy source link (unchanged, handled elsewhere),
a file citation (``path`` + optional ``line_start``/``line_end``/``quote``), or an event
citation (``event_ref`` + optional ``quote``). ``ingest_event`` is idempotent on the origin
event id, so re-normalizing a run (monitor, terminal rescan) never duplicates a finding or
its citations. ``record_verdict`` appends a citation verdict row; verdicts are insert-only,
chained by ``rerun_of_id`` so a re-check never overwrites what came before. Verdict
computation itself is a caller's job (the findings demo seed, and the verification
engine) - this service only stores what it is handed, with the method stated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agenttape.application.schemas.common import Page
from agenttape.application.schemas.findings import (
    CitationContentOut,
    FindingRowOut,
    FindingRunOut,
    FindingsTallyOut,
)
from agenttape.application.schemas.runs import RunOut
from agenttape.application.services.audit import record_audit
from agenttape.application.services.common import like_pattern, require_finished
from agenttape.application.services.dashboard import window_start
from agenttape.application.services.runs import RunService
from agenttape.context_evidence.common import outside_label
from agenttape.domain.enums import RunKind
from agenttape.domain.errors import ConflictError, NotFoundError, ValidationError
from agenttape.file_evidence.ledger import resolve_path
from agenttape.findings.citations import CITATION_VERDICTS, parse_citations
from agenttape.findings.normalize import has_redaction
from agenttape.findings.retention import BlobUnavailable, load_read
from agenttape.findings.review import (
    REVIEW_AUDIT_ACTION,
    REVIEW_SNAPSHOT_SECTION,
    parse_review_target,
    review_details,
)
from agenttape.findings.verify import NOT_CHECKABLE_REASONS
from agenttape.infrastructure.db.extra_models.findings import Citation, CitationVerdict, Finding
from agenttape.infrastructure.db.extra_models.verification import CitationReproduction, EvidenceRead
from agenttape.infrastructure.db.models import AuditEntry, Event, Run
from agenttape.security.redaction import DEFAULT_REDACTOR

if TYPE_CHECKING:
    from agenttape.application.context import AppContext

log = logging.getLogger(__name__)

MAX_METHOD_CHARS = 32

#: Query bounds for ``GET /citations/{id}/content`` (``context``/``before``/``after``).
MAX_CONTENT_CONTEXT = 200
#: The most lines the content endpoint ever returns in one response, on every path (see
#: ``_content_window``): context shrinks to fit, and a longer cited range, or a citation
#: with no range over a longer read, is cut to its first rows with ``clipped=True``.
MAX_CONTENT_LINES = 800

#: Audit actions the one human rung records: confirmation state is derived from the latest matching row, never a stored column.
CONFIRM_ACTION = "finding.confirm"
WITHDRAW_ACTION = "finding.withdraw_confirmation"

#: Worst first: identical to the frontend `VERDICT_RANK` (`frontend/src/features/findings/
#: model.ts`), so the inbox, the dashboard tile, and a run's own findings tab agree on what
#: a finding's worst verdict is. A citation whose latest verdict is null ranks "unchecked";
#: a finding with no citations at all ranks "declared_only".
INBOX_VERDICT_ORDER: tuple[str, ...] = (
    "quote_not_found",
    "never_read",
    "stale",
    "not_checkable",
    "read_not_observed",
    "unchecked",
    "declared_only",
    "quote_matches",
)

#: ``GET /findings``'s ``verdict`` filter: the six citation verdicts plus the finding-level
#: ``declared_only`` state (never a citation verdict itself).
INBOX_VERDICTS: tuple[str, ...] = (*CITATION_VERDICTS, "declared_only")


@dataclass
class _WindowRow:
    """One finding in a window, with its citation tally already computed: the shared shape
    ``list_across_runs`` and ``window_tally`` both build on."""

    finding: Finding
    run: Run
    citations: int
    #: Latest verdict of each citation, keyed by verdict; a null latest verdict counts as
    #: "unchecked". Only non-zero keys are present.
    verdict_counts: dict[str, int]
    worst_verdict: str


def _worst_verdict(verdict_counts: dict[str, int], citations: int) -> str:
    if citations == 0:
        return "declared_only"
    for verdict in INBOX_VERDICT_ORDER:
        if verdict == "declared_only":
            continue
        if verdict_counts.get(verdict, 0) > 0:
            return verdict
    return "quote_matches"  # unreachable: every citation counts somewhere above.


def _humanize_kind(kind: str) -> str:
    return kind.replace("_", " ").strip().capitalize() or kind


def _finding_run_out(run_out: RunOut) -> FindingRunOut:
    agent_label = run_out.agent_profile_name or run_out.adapter_type or _humanize_kind(run_out.kind)
    return FindingRunOut(
        id=run_out.id,
        title=run_out.title,
        status=run_out.status.value,
        kind=run_out.kind,
        agent_label=agent_label,
        project_id=run_out.project_id,
        project_name=run_out.project_name,
    )


#: Event types (and their preference order) an event-reference citation resolves against
#: when the ref does not directly name an AgentTape event id: the newest event of the run
#: matching this ``tool_use_id``, preferring a tool_result over a command over a tool_call.
_EVENT_REF_TYPE_PRIORITY = {"tool_result": 0, "command": 1, "tool_call": 2}


def _canonical_path(raw: str, workdir: Path | None) -> tuple[str, bool | None]:
    """Lexical canonicalization only: never opens, stats, or resolves ``raw`` on disk."""
    text = raw.strip()
    if workdir is None:
        return text, None
    resolved = resolve_path(text, workdir)
    if resolved is None:
        return text, None
    if resolved.inside:
        return resolved.path, True
    return outside_label(resolved.path), False


def _build_tool_use_map(session: Session, run_id: str) -> dict[str, str]:
    """``tool_use_id`` -> event id, for this run's tool_result/command/tool_call events.

    Built once per :meth:`FindingsService.ingest_event` call, only when the finding has an
    event citation, and entirely in Python: no JSON-path SQL, so SQLite and PostgreSQL agree.
    """
    rows = session.execute(
        select(Event.id, Event.type, Event.payload, Event.sequence).where(
            Event.run_id == run_id, Event.type.in_(tuple(_EVENT_REF_TYPE_PRIORITY))
        )
    ).all()
    best: dict[str, tuple[int, int]] = {}
    result: dict[str, str] = {}
    for event_id, event_type, payload, sequence in rows:
        ref = (payload or {}).get("tool_use_id")
        if not isinstance(ref, str) or not ref:
            continue
        rank = (_EVENT_REF_TYPE_PRIORITY[event_type], sequence)
        current = best.get(ref)
        if (
            current is None
            or rank[0] < current[0]
            or (rank[0] == current[0] and rank[1] > current[1])
        ):
            best[ref] = rank
            result[ref] = event_id
    return result


def _resolve_event_ref(
    session: Session, run_id: str, ref: str, tool_use_map: dict[str, str]
) -> str | None:
    """An AgentTape event id in ``run_id`` matching ``ref`` directly, or via ``tool_use_map``."""
    direct = session.execute(
        select(Event.id).where(Event.id == ref, Event.run_id == run_id)
    ).scalar_one_or_none()
    if direct is not None:
        return direct
    return tool_use_map.get(ref)


def latest_verdict(rows: list[CitationVerdict]) -> CitationVerdict | None:
    """The verdict row no other row of this citation names as ``rerun_of_id``.

    A forked chain (concurrent writers) falls back to the newest ``created_at``, then the
    highest id.
    """
    if not rows:
        return None
    superseded = {r.rerun_of_id for r in rows if r.rerun_of_id}
    candidates = [r for r in rows if r.id not in superseded] or rows
    return max(candidates, key=lambda r: (r.created_at, r.id))


def _reproduction_out(row: CitationReproduction) -> dict[str, Any]:
    return {
        "id": row.id,
        "trigger": row.trigger,
        "mode": row.mode,
        "status": row.status,
        "result": row.result,
        "reason": row.reason,
        "pinned_commit": row.pinned_commit,
        "exit_code": row.exit_code,
        "recorded_exit_code": row.recorded_exit_code,
        "created_at": row.created_at.isoformat(),
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


def _latest_reproduction(rows: list[CitationReproduction]) -> CitationReproduction | None:
    if not rows:
        return None
    return max(rows, key=lambda r: (r.created_at, r.id))


def _verdict_out(row: CitationVerdict) -> dict[str, Any]:
    return {
        "id": row.id,
        "verdict": row.verdict,
        "reason": row.reason,
        "method": row.method,
        "verifier_version": row.verifier_version,
        "evidence": row.evidence or {},
        "rerun_of_id": row.rerun_of_id,
        "created_at": row.created_at.isoformat(),
    }


def _covering_read(
    session: Session,
    run_id: str,
    path: str,
    before_sequence: int,
    claimed: tuple[int, int] | None,
) -> EvidenceRead | None:
    """The newest ``EvidenceRead`` of ``path`` in ``run_id`` recorded before ``before_sequence``
    that covers ``claimed``, or (with no claim, or no read covers it) simply the newest such
    read before the finding - never a read from another run, never a read reported after it.
    """
    rows = list(
        session.scalars(
            select(EvidenceRead)
            .join(Event, Event.id == EvidenceRead.result_event_id)
            .where(
                EvidenceRead.run_id == run_id,
                EvidenceRead.path == path,
                Event.sequence < before_sequence,
            )
            .order_by(Event.sequence.desc())
        )
    )
    if not rows:
        return None
    if claimed is not None:
        for read in rows:
            if (
                read.line_first is not None
                and read.line_last is not None
                and read.line_first <= claimed[0]
                and read.line_last >= claimed[1]
            ):
                return read
    return rows[0]


def _unavailable_content(
    citation: Citation, reason: str, *, blob_ref: str | None = None
) -> CitationContentOut:
    return CitationContentOut(
        path=citation.path or "",
        lines=[],
        line_first=None,
        line_last=None,
        window=None,
        cited=None,
        truncated=False,
        redacted=False,
        clipped=False,
        blob_ref=blob_ref,
        read_result_event_id=None,
        resolved_by=None,
        unavailable_reason=reason,
    )


def _content_window(
    cited: tuple[int, int] | None,
    line_first: int | None,
    line_last: int | None,
    before: int,
    after: int,
) -> tuple[int | None, int | None, bool]:
    """The ``(start, end, clipped)`` line window ``citation_content`` serves (T28).

    Every path returns at most ``MAX_CONTENT_LINES`` rows. A citation with no line range
    gets the first ``MAX_CONTENT_LINES`` lines of the retained read. A cited range longer
    than the cap gets its first ``MAX_CONTENT_LINES`` lines. Otherwise the cited range is
    served whole and the requested context shrinks (evenly, where the read allows) so the
    total still fits. ``clipped`` is true exactly when a line the citation points at (every
    retained line, for a citation with no range) was withheld by the cap.
    """
    if line_first is None or line_last is None:
        return None, None, False
    if cited is None:
        end = min(line_last, line_first + MAX_CONTENT_LINES - 1)
        return line_first, end, end < line_last
    cited_start, cited_end = cited
    if cited_end - cited_start + 1 > MAX_CONTENT_LINES:
        end = cited_start + MAX_CONTENT_LINES - 1
        return max(line_first, cited_start), min(line_last, end), True
    budget = MAX_CONTENT_LINES - (cited_end - cited_start + 1)
    room_before = max(0, min(before, cited_start - line_first))
    room_after = max(0, min(after, line_last - cited_end))
    take_before = min(room_before, max(budget // 2, budget - room_after))
    take_after = min(room_after, budget - take_before)
    start = max(line_first, cited_start - take_before)
    end = min(line_last, cited_end + take_after)
    return start, end, False


def _locked_finding(session: Session, finding_id: str) -> Finding:
    """The finding a confirm/withdraw request acts on, read ``FOR UPDATE`` so two requests
    for one finding serialize on databases with row locks (Postgres: the second waits for
    the first to commit, then sees its row). SQLite renders no ``FOR UPDATE``; there the
    one-writer lock plus ``_append_confirmation_row``'s re-check does the same job."""
    finding = session.scalars(
        select(Finding).where(Finding.id == finding_id).with_for_update()
    ).first()
    if finding is None:
        raise NotFoundError("finding", finding_id)
    return finding


def _append_confirmation_row(
    session: Session,
    ctx: AppContext,
    finding: Finding,
    *,
    action: str,
    actor: str,
    note: str | None,
) -> None:
    """Append one ``CONFIRM_ACTION``/``WITHDRAW_ACTION`` audit row and re-check, inside the
    same transaction, that it is a real transition. Once the row is flushed
    this transaction holds the database's write lock (SQLite) or the finding's row lock
    (Postgres), so every other request for this finding has either committed already, and
    is visible here, or is waiting. If our row is not now the latest, or the rows before it
    do not leave the finding in the state this action starts from (unconfirmed for a
    confirm, confirmed for a withdraw), a racing request won: raising rolls our row back and
    the caller gets the same 409 a sequential repeat gets, so the append-only log never
    holds a duplicate confirmation or withdrawal."""
    entry = record_audit(
        session,
        ctx.clock,
        action=action,
        resource_type="finding",
        resource_id=finding.id,
        actor=actor,
        run_id=finding.run_id,
        details={"finding_key": finding.finding_key, "note": note},
    )
    session.flush()
    rows = sorted(
        session.scalars(
            select(AuditEntry).where(
                AuditEntry.resource_type == "finding",
                AuditEntry.resource_id == finding.id,
                AuditEntry.action.in_((CONFIRM_ACTION, WITHDRAW_ACTION)),
            )
        ),
        key=lambda row: (row.ts, row.id),
    )
    earlier = [row for row in rows if row.id != entry.id]
    was_confirmed = bool(earlier) and earlier[-1].action == CONFIRM_ACTION
    if rows[-1].id != entry.id or was_confirmed != (action == WITHDRAW_ACTION):
        raise ConflictError(
            "This finding is already confirmed"
            if action == CONFIRM_ACTION
            else "This finding is not confirmed"
        )


def _confirmations(session: Session, finding_ids: list[str]) -> dict[str, dict[str, Any] | None]:
    """The latest ``finding.confirm``/``finding.withdraw_confirmation`` audit row per finding
    id, keyed by finding id: a confirm row yields ``{actor, ts, note}``, a withdraw row (or no
    row at all) yields ``None``. One query, no stored column."""
    if not finding_ids:
        return {}
    rows = list(
        session.scalars(
            select(AuditEntry).where(
                AuditEntry.resource_type == "finding",
                AuditEntry.resource_id.in_(finding_ids),
                AuditEntry.action.in_((CONFIRM_ACTION, WITHDRAW_ACTION)),
            )
        )
    )
    latest: dict[str, AuditEntry] = {}
    for row in rows:
        assert row.resource_id is not None
        current = latest.get(row.resource_id)
        if current is None or (row.ts, row.id) > (current.ts, current.id):
            latest[row.resource_id] = row
    return {
        finding_id: (
            {
                "actor": row.actor,
                "ts": row.ts.isoformat(),
                "note": (row.details or {}).get("note"),
            }
            if row.action == CONFIRM_ACTION
            else None
        )
        for finding_id, row in latest.items()
    }


def trusted_challenge_section(session: Session, run: Run) -> dict[str, Any] | None:
    """The run's challenge snapshot section, trusted only with its provenance record.

    ``RunService.create_run`` writes the section only from ``FindingReviewService`` (a
    caller-supplied one is refused), and in the same transaction appends the
    ``REVIEW_AUDIT_ACTION`` audit entry naming the same target run. Defense in depth
    (docs/SECURITY.md T27): a run whose snapshot carries the section by any other route
    has no such entry, so its review-shaped findings stay unresolved (``not_a_review_run``)
    and ``FindingReviewService.challenges`` does not list it on either of its routes (an
    on-demand child or a workflow-node sibling).
    """
    section = (run.snapshot or {}).get(REVIEW_SNAPSHOT_SECTION)
    if not isinstance(section, dict):
        return None
    provenance = session.scalars(
        select(AuditEntry.details).where(
            AuditEntry.action == REVIEW_AUDIT_ACTION,
            AuditEntry.resource_type == "run",
            AuditEntry.resource_id == run.id,
        )
    ).all()
    target_run_id = section.get("target_run_id")
    if not any((d or {}).get("target_run_id") == target_run_id for d in provenance):
        return None
    return section


class FindingsService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    @staticmethod
    def ingest_event(session: Session, run: Run, event: Event, workdir: Path | None) -> Finding:
        """Idempotent on ``origin_event_id``: returns the existing row when present."""
        existing = session.scalars(
            select(Finding).where(Finding.origin_event_id == event.id)
        ).first()
        if existing is not None:
            return existing
        payload = dict(event.payload or {})
        #: The event sink replaces an over-cap payload with this placeholder (nothing about
        #: the finding's own content survives it), or truncates an over-cap "text" field in
        #: place and sets "text_truncated" (docs/EVENT-SCHEMA.md "Size caps"). Either form
        #: means the stored finding text is not the agent's full claim.
        payload_truncated = (
            payload.get("truncated") is True or payload.get("text_truncated") is True
        )
        finding_key = str(payload.get("id") or event.id)[:200]
        clashes = session.scalars(
            select(Finding.id).where(Finding.run_id == run.id, Finding.finding_key == finding_key)
        ).first()
        if clashes is not None:
            #: The origin event id is globally unique (unlike the sequence, which is only
            #: unique within the run), so an agent-chosen id crafted to look like
            #: "{id}@{n}" can never collide with the disambiguated key this produces.
            finding_key = f"{finding_key}@{event.id}"[:200]
        kind = str(payload.get("kind") or "finding")[:32]
        text = DEFAULT_REDACTOR.redact_text(str(payload.get("text") or event.summary or ""))[:4000]
        drafts, dropped = parse_citations(payload.get("links"))
        details: dict[str, Any] = {}
        if dropped:
            details["citations_dropped"] = dropped
        if payload_truncated:
            details["payload_truncated"] = True
        review_draft = parse_review_target(payload)
        if review_draft is not None:

            def find_target(target_run_id: str, target_finding_key: str) -> str | None:
                return session.execute(
                    select(Finding.id).where(
                        Finding.run_id == target_run_id, Finding.finding_key == target_finding_key
                    )
                ).scalar_one_or_none()

            details["review"] = review_details(
                review_draft, trusted_challenge_section(session, run), find_target
            )
        finding = Finding(
            run_id=run.id,
            attempt_id=event.attempt_id,
            node_key=event.node_key,
            origin_event_id=event.id,
            sequence=event.sequence,
            finding_key=finding_key,
            kind=kind,
            text=text,
            source=event.source,
            details=details,
            created_at=event.ts,
        )
        session.add(finding)
        session.flush()

        tool_use_map: dict[str, str] | None = None
        for position, draft in enumerate(drafts):
            path, inside_boundary = (
                _canonical_path(draft.path, workdir) if draft.path is not None else (None, None)
            )
            event_id: str | None = None
            if draft.event_ref is not None:
                if tool_use_map is None:
                    tool_use_map = _build_tool_use_map(session, run.id)
                event_id = _resolve_event_ref(session, run.id, draft.event_ref, tool_use_map)
            quote = DEFAULT_REDACTOR.redact_text(draft.quote) if draft.quote is not None else None
            #: The event sink already redacts the whole payload before this runs, so a
            #: second pass here is idempotent; check the marker itself rather than a
            #: before/after diff, so a citation whose quote was redacted upstream still
            #: reports it.
            quote_redacted = quote is not None and has_redaction(quote)
            event_ref = (
                DEFAULT_REDACTOR.redact_text(draft.event_ref)
                if draft.event_ref is not None
                else None
            )
            citation_details: dict[str, Any] = {"relation": draft.relation}
            if draft.quote_truncated:
                citation_details["quote_truncated"] = True
            if quote_redacted:
                citation_details["quote_redacted"] = True
            session.add(
                Citation(
                    finding_id=finding.id,
                    position=position,
                    kind=draft.kind,
                    path=path,
                    inside_boundary=inside_boundary,
                    line_start=draft.line_start,
                    line_end=draft.line_end,
                    quote=quote,
                    event_ref=event_ref,
                    event_id=event_id,
                    details=citation_details,
                    created_at=event.ts,
                )
            )
        return finding

    @staticmethod
    def findings_for_run(session: Session, run_id: str) -> list[dict[str, Any]]:
        rows = list(
            session.scalars(
                select(Finding).where(Finding.run_id == run_id).order_by(Finding.sequence)
            )
        )
        if not rows:
            return []
        citation_rows = list(
            session.scalars(
                select(Citation)
                .where(Citation.finding_id.in_([f.id for f in rows]))
                .order_by(Citation.finding_id, Citation.position)
            )
        )
        by_finding: dict[str, list[Citation]] = {}
        for c in citation_rows:
            by_finding.setdefault(c.finding_id, []).append(c)

        verdicts_by_citation: dict[str, list[CitationVerdict]] = {}
        reproductions_by_citation: dict[str, list[CitationReproduction]] = {}
        if citation_rows:
            for v in session.scalars(
                select(CitationVerdict).where(
                    CitationVerdict.citation_id.in_([c.id for c in citation_rows])
                )
            ):
                verdicts_by_citation.setdefault(v.citation_id, []).append(v)
            for r in session.scalars(
                select(CitationReproduction).where(
                    CitationReproduction.citation_id.in_([c.id for c in citation_rows])
                )
            ):
                reproductions_by_citation.setdefault(r.citation_id, []).append(r)

        confirmations = _confirmations(session, [f.id for f in rows])

        def citation_out(c: Citation) -> dict[str, Any]:
            citation_verdicts = verdicts_by_citation.get(c.id, [])
            latest = latest_verdict(citation_verdicts)
            citation_reproductions = reproductions_by_citation.get(c.id, [])
            latest_reproduction = _latest_reproduction(citation_reproductions)
            return {
                "id": c.id,
                "position": c.position,
                "kind": c.kind,
                "path": c.path,
                "inside_boundary": c.inside_boundary,
                "line_start": c.line_start,
                "line_end": c.line_end,
                "quote": c.quote,
                "event_ref": c.event_ref,
                "event_id": c.event_id,
                "relation": (c.details or {}).get("relation"),
                "details": c.details or {},
                "verdict": _verdict_out(latest) if latest is not None else None,
                "checks": len(citation_verdicts),
                "reproduction": (
                    _reproduction_out(latest_reproduction)
                    if latest_reproduction is not None
                    else None
                ),
                "reproductions": len(citation_reproductions),
            }

        out: list[dict[str, Any]] = []
        for f in rows:
            cites = by_finding.get(f.id, [])
            out.append(
                {
                    "id": f.id,
                    "key": f.finding_key,
                    "kind": f.kind,
                    "text": f.text,
                    "source": f.source,
                    "event_id": f.origin_event_id,
                    "sequence": f.sequence,
                    "ts": f.created_at.isoformat(),
                    "attempt_id": f.attempt_id,
                    "node_key": f.node_key,
                    "details": f.details or {},
                    "declared_only": len(cites) == 0,
                    "citations": [citation_out(c) for c in cites],
                    "confirmation": confirmations.get(f.id),
                }
            )
        return out

    def _window_rows(
        self,
        session: Session,
        *,
        days: int,
        project_id: str | None = None,
        agent_profile_id: str | None = None,
        kind: str | None = None,
        source: str | None = None,
        q: str | None = None,
    ) -> list[_WindowRow]:
        """Every finding recorded across every run in the window, with its own citation
        tally: the one cross-run read model behind both ``list_across_runs`` and
        ``window_tally``. Filters are applied in SQL; citations
        and their latest verdicts are loaded in the same batched-``IN`` pattern
        ``findings_for_run`` uses, never a per-row query.
        """
        start = window_start(self.ctx.clock, days)
        stmt = (
            select(Finding, Run)
            .join(Run, Run.id == Finding.run_id)
            .where(Finding.created_at >= start)
        )
        if project_id:
            stmt = stmt.where(Run.project_id == project_id)
        if agent_profile_id:
            stmt = stmt.where(Run.agent_profile_id == agent_profile_id)
        if kind:
            stmt = stmt.where(Finding.kind == kind)
        if source == "launched":
            stmt = stmt.where(Run.kind == RunKind.AGENT.value)
        elif source == "observed":
            stmt = stmt.where(Run.kind == RunKind.OBSERVED.value)
        if q:
            stmt = stmt.where(func.lower(Finding.text).like(like_pattern(q), escape="\\"))
        pairs = list(session.execute(stmt).all())
        if not pairs:
            return []
        findings = [f for f, _r in pairs]
        runs_by_finding_id = {f.id: r for f, r in pairs}

        citation_rows = list(
            session.scalars(
                select(Citation).where(Citation.finding_id.in_([f.id for f in findings]))
            )
        )
        by_finding: dict[str, list[Citation]] = {}
        for c in citation_rows:
            by_finding.setdefault(c.finding_id, []).append(c)

        verdicts_by_citation: dict[str, list[CitationVerdict]] = {}
        if citation_rows:
            for v in session.scalars(
                select(CitationVerdict).where(
                    CitationVerdict.citation_id.in_([c.id for c in citation_rows])
                )
            ):
                verdicts_by_citation.setdefault(v.citation_id, []).append(v)

        out: list[_WindowRow] = []
        for finding in findings:
            cites = by_finding.get(finding.id, [])
            verdict_counts: dict[str, int] = {}
            for c in cites:
                latest = latest_verdict(verdicts_by_citation.get(c.id, []))
                key = latest.verdict if latest is not None else "unchecked"
                verdict_counts[key] = verdict_counts.get(key, 0) + 1
            out.append(
                _WindowRow(
                    finding=finding,
                    run=runs_by_finding_id[finding.id],
                    citations=len(cites),
                    verdict_counts=verdict_counts,
                    worst_verdict=_worst_verdict(verdict_counts, len(cites)),
                )
            )
        return out

    def list_across_runs(
        self,
        *,
        days: int,
        project_id: str | None = None,
        agent_profile_id: str | None = None,
        verdict: str | None = None,
        kind: str | None = None,
        source: str | None = None,
        q: str | None = None,
        sort: str = "flagged",
        limit: int = 50,
        offset: int = 0,
    ) -> Page[FindingRowOut]:
        """``GET /findings``: every finding recorded across every run in the window, worst
        verdict first by default - the inbox for interactive
        Claude Code sessions nobody opens on purpose."""
        with self.ctx.session() as session:
            rows = self._window_rows(
                session,
                days=days,
                project_id=project_id,
                agent_profile_id=agent_profile_id,
                kind=kind,
                source=source,
                q=q,
            )
            if verdict:
                if verdict == "declared_only":
                    rows = [r for r in rows if r.citations == 0]
                else:
                    rows = [r for r in rows if r.verdict_counts.get(verdict, 0) > 0]
            if sort == "newest":
                rows.sort(key=lambda r: (r.finding.created_at, r.finding.id), reverse=True)
            else:
                rank = {v: i for i, v in enumerate(INBOX_VERDICT_ORDER)}
                rows.sort(
                    key=lambda r: (
                        rank[r.worst_verdict],
                        -r.finding.created_at.timestamp(),
                    )
                )
            total = len(rows)
            page_rows = rows[offset : offset + limit]
            run_service = RunService(self.ctx)
            items = [
                FindingRowOut(
                    id=r.finding.id,
                    key=r.finding.finding_key,
                    kind=r.finding.kind,
                    text=r.finding.text,
                    created_at=r.finding.created_at,
                    citations=r.citations,
                    verdict_counts=r.verdict_counts,
                    worst_verdict=r.worst_verdict,
                    run=_finding_run_out(run_service._run_out(session, r.run)),
                )
                for r in page_rows
            ]
            next_offset = offset + limit if offset + limit < total else None
            return Page[FindingRowOut](
                items=items, total=total, limit=limit, offset=offset, next_offset=next_offset
            )

    def window_tally(self, days: int) -> FindingsTallyOut:
        """The ``findings`` block of ``GET /dashboard``: the same read model as
        ``list_across_runs``, unfiltered, tallied instead of listed - no second cross-run query."""
        with self.ctx.session() as session:
            rows = self._window_rows(session, days=days)
        declared_only = 0
        all_quotes_match = 0
        citations = 0
        verdicts: dict[str, int] = {}
        unchecked = 0
        for r in rows:
            citations += r.citations
            if r.citations == 0:
                declared_only += 1
                continue
            if r.verdict_counts.get("quote_matches", 0) == r.citations:
                all_quotes_match += 1
            for v, count in r.verdict_counts.items():
                if v == "unchecked":
                    unchecked += count
                else:
                    verdicts[v] = verdicts.get(v, 0) + count
        return FindingsTallyOut(
            recorded=len(rows),
            all_quotes_match=all_quotes_match,
            declared_only=declared_only,
            citations=citations,
            verdicts=verdicts,
            unchecked=unchecked,
        )

    def citation_content(
        self, citation_id: str, *, before: int = 3, after: int = 3
    ) -> CitationContentOut:
        """Cited lines from the retained read ``citation_id`` was checked against, scoped to
        its own run. Never reads the file from disk, never falls
        back to the agent's quote, and never resolves a read by path across runs.

        Resolution order: (1) the latest verdict's ``evidence.read.result_event_id`` to an
        ``EvidenceRead`` with that id and this citation's run id (``resolved_by
        ="verdict_read"``); (2) otherwise the newest covering (or, failing that, newest)
        ``EvidenceRead`` of the same path recorded before the finding (``resolved_by
        ="covering_read"``); (3) none - ``unavailable_reason="no_retained_read"``.
        """
        with self.ctx.session() as session:
            citation = session.get(Citation, citation_id)
            if citation is None:
                raise NotFoundError("citation", citation_id)
            finding = session.get(Finding, citation.finding_id)
            assert finding is not None
            run_id = finding.run_id

            if citation.kind == "event":
                raise ValidationError(
                    "Event citations render from the recorded event; only a file citation "
                    "has retained lines"
                )
            if citation.inside_boundary is False:
                return _unavailable_content(citation, "outside_boundary")

            claimed: tuple[int, int] | None = None
            if citation.line_start is not None:
                end = citation.line_end if citation.line_end is not None else citation.line_start
                claimed = (citation.line_start, end)

            verdict_rows = list(
                session.scalars(
                    select(CitationVerdict).where(CitationVerdict.citation_id == citation_id)
                )
            )
            latest = latest_verdict(verdict_rows)
            evidence = (latest.evidence or {}) if latest is not None else {}
            read_info = evidence.get("read")
            if not isinstance(read_info, dict):
                read_info = None

            cited = claimed
            line_range = evidence.get("line_range")
            if isinstance(line_range, list) and len(line_range) == 2:
                cited = (line_range[0], line_range[1])

            read: EvidenceRead | None = None
            resolved_by: Literal["verdict_read", "covering_read"] | None = None
            if read_info is not None and read_info.get("result_event_id"):
                read = session.scalars(
                    select(EvidenceRead).where(
                        EvidenceRead.result_event_id == read_info["result_event_id"],
                        EvidenceRead.run_id == run_id,
                    )
                ).first()
                if read is not None:
                    resolved_by = "verdict_read"

            if read is None:
                read = _covering_read(
                    session, run_id, citation.path or "", finding.sequence, claimed
                )
                if read is not None:
                    resolved_by = "covering_read"

            if read is None:
                return _unavailable_content(citation, "no_retained_read")

            try:
                loaded = load_read(self.ctx, session, read)
            except BlobUnavailable as exc:
                log.info("citation %s: retained content unavailable (%s)", citation_id, exc.detail)
                return _unavailable_content(
                    citation, "blob_unavailable", blob_ref=exc.blob_ref_value
                )

            numbered = loaded.numbered_lines
            line_first = numbered[0][0] if numbered else None
            line_last = numbered[-1][0] if numbered else None

            window_start, window_end, clipped = _content_window(
                cited, line_first, line_last, before, after
            )

            raw_lines = [
                (n, t)
                for n, t in numbered
                if window_start is not None
                and window_end is not None
                and window_start <= n <= window_end
            ]
            lines = [(n, DEFAULT_REDACTOR.redact_text(t)) for n, t in raw_lines]
            redacted = loaded.redacted or any(
                original != new for (_, original), (_, new) in zip(raw_lines, lines, strict=True)
            )

            window: tuple[int, int] | None = None
            if lines and window_start is not None and window_end is not None:
                window = (window_start, window_end)

            return CitationContentOut(
                path=citation.path or "",
                lines=lines,
                line_first=line_first,
                line_last=line_last,
                window=window,
                cited=cited,
                truncated=loaded.truncated,
                redacted=redacted,
                clipped=clipped,
                blob_ref=loaded.blob_ref,
                read_result_event_id=read.result_event_id,
                resolved_by=resolved_by,
                unavailable_reason=None,
            )

    def record_verdict(
        self,
        citation_id: str,
        *,
        verdict: str,
        method: str,
        reason: str | None = None,
        evidence: dict[str, Any] | None = None,
        verifier_version: str | None = None,
    ) -> dict[str, Any]:
        """Append a verdict row for ``citation_id``. Insert-only: never updates or deletes an
        existing row; a re-check's ``rerun_of_id`` names the verdict it supersedes. A
        ``not_checkable`` verdict must carry ``evidence["not_checkable_reason"]`` from the
        closed ``NOT_CHECKABLE_REASONS``."""
        if verdict not in CITATION_VERDICTS:
            raise ValidationError(
                f"'{verdict}' is not a citation verdict", details={"verdict": verdict}
            )
        if not isinstance(method, str) or not (1 <= len(method) <= MAX_METHOD_CHARS):
            raise ValidationError(
                "method is required and must be 1-32 characters", details={"method": method}
            )
        if verdict == "not_checkable":
            code = (evidence or {}).get("not_checkable_reason")
            if code not in NOT_CHECKABLE_REASONS:
                raise ValidationError(
                    "A not_checkable verdict must name a known not_checkable_reason",
                    details={"not_checkable_reason": code, "allowed": list(NOT_CHECKABLE_REASONS)},
                )
        with self.ctx.session() as session:
            citation = session.get(Citation, citation_id)
            if citation is None:
                raise NotFoundError("citation", citation_id)
            existing = list(
                session.scalars(
                    select(CitationVerdict).where(CitationVerdict.citation_id == citation_id)
                )
            )
            latest = latest_verdict(existing)
            row = CitationVerdict(
                citation_id=citation_id,
                verdict=verdict,
                reason=reason,
                method=method,
                verifier_version=verifier_version,
                evidence=evidence or {},
                rerun_of_id=latest.id if latest is not None else None,
                created_at=self.ctx.clock.now(),
            )
            session.add(row)
            session.flush()
            return _verdict_out(row)

    def confirm(self, finding_id: str, *, actor: str, note: str | None = None) -> dict[str, Any]:
        """A person confirms a finding: the one human rung on the evidence ladder. Refuses a
        finding whose run has not finished (nothing to review yet) and a finding already
        confirmed (withdraw first). Appends ``CONFIRM_ACTION`` to the audit log; there is no
        migration and no stored column."""
        with self.ctx.session() as session:
            finding = _locked_finding(session, finding_id)
            run = session.get(Run, finding.run_id)
            assert run is not None
            require_finished(run, "A finding can be confirmed once its run has finished")
            if _confirmations(session, [finding.id]).get(finding.id) is not None:
                raise ConflictError("This finding is already confirmed")
            _append_confirmation_row(
                session, self.ctx, finding, action=CONFIRM_ACTION, actor=actor, note=note
            )
            return {
                "finding_id": finding.id,
                "confirmation": _confirmations(session, [finding.id]).get(finding.id),
            }

    def withdraw_confirmation(
        self, finding_id: str, *, actor: str, note: str | None = None
    ) -> dict[str, Any]:
        """Withdraws a finding's confirmation: appends ``WITHDRAW_ACTION`` to the audit log
        (never deletes or edits the confirming row) so the full history stays in ``/audit``;
        the finding and its citation verdicts are never touched. Refuses a finding that is not
        currently confirmed."""
        with self.ctx.session() as session:
            finding = _locked_finding(session, finding_id)
            if _confirmations(session, [finding.id]).get(finding.id) is None:
                raise ConflictError("This finding is not confirmed")
            _append_confirmation_row(
                session, self.ctx, finding, action=WITHDRAW_ACTION, actor=actor, note=note
            )
            return {"finding_id": finding.id, "confirmation": None}
