"""Run-scoped, Session-aware read lookups over ``file_evidence_events`` and ``events``.

The one place verification queries the database. Every query is filtered by the citing
run's ``run_id``: reads come from ``file_evidence_events`` (so a citation and the File
Evidence tab always agree on what was read), and content comes from the raw
``tool_call``/``tool_result`` event pair the read row's ``source_event_id`` names. Nothing
here re-canonicalizes a path: ``FileEvidenceEvent.path`` and ``Citation.path`` are already
canonicalized the same way (``file_evidence.ledger.resolve_path``), by the same code path
that wrote each of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from agenttape.artifacts.store import ArtifactStore
from agenttape.domain.enums import EventType
from agenttape.file_evidence.ledger import WRITE_OPS
from agenttape.file_evidence.store import completeness
from agenttape.findings.coverage import observed_coverage
from agenttape.findings.retained_output import RETAINED_KEY, RetainedOutputRef, load_output
from agenttape.findings.verify import (
    EventView,
    EvidenceWindow,
    WriteView,
    evidence_window,
)
from agenttape.infrastructure.db.extra_models.file_evidence import (
    FileEvidenceEvent,
    FileEvidenceSnapshot,
)
from agenttape.infrastructure.db.models import Event, Run


@dataclass(frozen=True)
class ReadSource:
    evidence_id: str
    path: str
    ts: datetime
    sequence: int
    source: str
    source_event_id: str | None
    call_event_id: str | None
    result_event_id: str | None
    result_sequence: int | None
    tool: str | None
    offset: int | None
    limit: int | None
    output: str | None
    output_truncated: bool
    is_error: bool
    #: The read's full redacted output kept beyond the event text cap, when the recorder
    #: kept one (``findings/retained_output.py``); ``output`` is then the capped event text.
    retained: RetainedOutputRef | None = None


def _tool_results_by_parent(session: Session, run_id: str) -> dict[str, Event]:
    """This run's tool_result events keyed by ``parent_event_id`` (lowest sequence wins).

    One query per run, built once by :func:`reads_for_path`'s caller-visible cost, not once
    per row or per citation.
    """
    rows = list(
        session.scalars(
            select(Event)
            .where(
                Event.run_id == run_id,
                Event.type == EventType.TOOL_RESULT.value,
                Event.parent_event_id.is_not(None),
            )
            .order_by(Event.sequence)
        )
    )
    by_parent: dict[str, Event] = {}
    for row in rows:
        parent_id = row.parent_event_id
        if parent_id is not None and parent_id not in by_parent:
            by_parent[parent_id] = row
    return by_parent


def reads_for_path(
    session: Session, run_id: str, path: str, *, before_sequence: int
) -> list[ReadSource]:
    """Reads of ``path`` in ``run_id`` that could back a citation whose origin event's
    sequence is ``before_sequence`` (a read reported after the citing finding cannot back it).

    Rows whose ``source_event_id`` names a ``tool_call`` event in this run get their content
    from that call's paired ``tool_result`` (``payload["output"]`` when it is a string,
    ``output_truncated`` when the result was cut or replaced by the whole-payload
    placeholder, and ``offset``/``limit`` from the call's own ``input`` when they are ints);
    such a row is dropped entirely when the call's sequence is not before ``before_sequence``.
    A row with no resolvable ``tool_call`` (no source event, or a non-tool source such as
    ``agent_hook``) is kept with ``output=None`` - it can never back a ``quote_matches``
    verdict, but its presence still reflects that File Evidence saw the file read.

    On an observed or Herdr-hosted run, sequence is the order AgentTape received the session's hooks and
    MCP findings (both are written by one ingest thread,
    ``application/services/hook_ingest.py``), so a
    read whose hooks were still queued when the finding arrived is still before it.
    """
    rows = list(
        session.scalars(
            select(FileEvidenceEvent)
            .where(
                FileEvidenceEvent.run_id == run_id,
                FileEvidenceEvent.path == path,
                FileEvidenceEvent.operation == "read",
            )
            .order_by(FileEvidenceEvent.ts, FileEvidenceEvent.sequence)
        )
    )
    if not rows:
        return []

    call_ids = {row.source_event_id for row in rows if row.source_event_id}
    calls_by_id: dict[str, Event] = {}
    if call_ids:
        for call_row in session.scalars(
            select(Event).where(Event.run_id == run_id, Event.id.in_(call_ids))
        ):
            calls_by_id[call_row.id] = call_row
    results_by_parent = _tool_results_by_parent(session, run_id)

    out: list[ReadSource] = []
    for row in rows:
        call = calls_by_id.get(row.source_event_id) if row.source_event_id else None
        if call is None or call.type != EventType.TOOL_CALL.value:
            out.append(
                ReadSource(
                    evidence_id=row.id,
                    path=row.path,
                    ts=row.ts,
                    sequence=row.sequence,
                    source=row.source,
                    source_event_id=row.source_event_id,
                    call_event_id=None,
                    result_event_id=None,
                    result_sequence=None,
                    tool=row.tool,
                    offset=None,
                    limit=None,
                    output=None,
                    output_truncated=False,
                    is_error=False,
                )
            )
            continue
        if call.sequence >= before_sequence:
            continue
        result = results_by_parent.get(call.id)
        output: str | None = None
        output_truncated = False
        is_error = False
        retained: RetainedOutputRef | None = None
        result_event_id: str | None = None
        result_sequence: int | None = None
        if result is not None:
            result_event_id = result.id
            result_sequence = result.sequence
            payload: dict[str, Any] = result.payload or {}
            raw_output = payload.get("output")
            if isinstance(raw_output, str):
                output = raw_output
            output_truncated = bool(payload.get("output_truncated")) or bool(
                payload.get("truncated")
            )
            is_error = bool(payload.get("is_error"))
            retained = RetainedOutputRef.from_payload(payload.get(RETAINED_KEY))
        call_input = (call.payload or {}).get("input")
        offset: int | None = None
        limit: int | None = None
        if isinstance(call_input, dict):
            raw_offset = call_input.get("offset")
            raw_limit = call_input.get("limit")
            if isinstance(raw_offset, int) and not isinstance(raw_offset, bool):
                offset = raw_offset
            if isinstance(raw_limit, int) and not isinstance(raw_limit, bool):
                limit = raw_limit
        out.append(
            ReadSource(
                evidence_id=row.id,
                path=row.path,
                ts=row.ts,
                sequence=row.sequence,
                source=row.source,
                source_event_id=row.source_event_id,
                call_event_id=call.id,
                result_event_id=result_event_id,
                result_sequence=result_sequence,
                tool=row.tool,
                offset=offset,
                limit=limit,
                output=output,
                output_truncated=output_truncated,
                is_error=is_error,
                retained=retained,
            )
        )
    return out


def writes_after(session: Session, run_id: str, path: str, read: ReadSource) -> list[WriteView]:
    """Writes (or renames) to ``path`` in ``run_id`` recorded strictly after ``read``.

    Ordered by ``(ts, sequence)`` - the same total order File Evidence's own ledger uses -
    and compared against ``read``'s own ``(ts, sequence)``, never a live filesystem/git diff
    (the run's worktree may already be gone by verification time). A write whose
    ``previous_path`` names ``path`` (a rename away from it) counts too.
    """
    rows = list(
        session.scalars(
            select(FileEvidenceEvent)
            .where(
                FileEvidenceEvent.run_id == run_id,
                FileEvidenceEvent.operation.in_(WRITE_OPS),
                or_(FileEvidenceEvent.path == path, FileEvidenceEvent.previous_path == path),
            )
            .order_by(FileEvidenceEvent.ts, FileEvidenceEvent.sequence)
        )
    )
    out: list[WriteView] = []
    for row in rows:
        if (row.ts, row.sequence) <= (read.ts, read.sequence):
            continue
        out.append(
            WriteView(
                evidence_id=row.id,
                source_event_id=row.source_event_id,
                operation=row.operation,
                path=row.path,
                previous_path=row.previous_path,
                ts=row.ts.isoformat(),
                sequence=row.sequence,
                source=row.source,
                before_hash=row.before_hash,
                after_hash=row.after_hash,
            )
        )
    return out


def start_hash(session: Session, run_id: str, path: str) -> str | None:
    """The Git blob hash recorded for ``path`` in the run's first start snapshot, if any.

    ``None`` when there is no start snapshot, or ``path`` was not recorded in it - never
    fabricated. The first (earliest ``taken_at``) start snapshot is used, matching how File
    Evidence itself treats the run's start state.
    """
    row = session.scalars(
        select(FileEvidenceSnapshot)
        .where(FileEvidenceSnapshot.run_id == run_id, FileEvidenceSnapshot.kind == "start")
        .order_by(FileEvidenceSnapshot.taken_at)
        .limit(1)
    ).first()
    if row is None:
        return None
    entry = (row.files or {}).get(path)
    if not isinstance(entry, list) or not entry:
        return None
    value = entry[0]
    return value if isinstance(value, str) else None


def run_window(session: Session, run: Run) -> EvidenceWindow:
    """The run's evidence-window completeness, as :func:`decide_file_citation` needs it.

    Built by :func:`agenttape.findings.verify.evidence_window` from ``completeness()``'s
    ``reads``/``offline_windows``/``statement`` and, for a run read only through Claude Code
    hooks (observed or Herdr-hosted), its
    :func:`agenttape.findings.coverage.observed_coverage`; this function never re-derives ``completeness()``'s own
    level/statement logic, only reads the fields it needs.
    """
    result = completeness(session, run)
    return evidence_window(
        reads=str(result.get("reads") or "unavailable"),
        offline_windows=[dict(w) for w in (result.get("offline_windows") or ())],
        statement=str(result.get("statement") or ""),
        observed=observed_coverage(session, run),
    )


#: Priority order for an event citation's content, matching the order
#: :func:`decide_event_citation` should prefer when an event carries more than one of these
#: keys (a COMMAND event's ``output``, a diff, an assistant ``text``, etc.).
_EVENT_CONTENT_KEYS = ("output", "diff", "text", "content", "body", "message")


def event_view(
    session: Session,
    run_id: str,
    event_id: str | None,
    *,
    artifact_store: ArtifactStore | None = None,
) -> EventView | None:
    """The event ``event_id`` resolved to in ``run_id``, or ``None`` when it does not exist.

    A read's ``tool_result`` that carries a retained output (``output_retained``, set only
    by the recorder) is checked against that copy, loaded by hash from ``run_id``'s own
    evidence directory through ``artifact_store`` (``retained=True``), exactly as a file
    citation of the same read is; when the copy does not load, the event's own capped
    content is used, as before.

    Scoped by ``run_id`` in the query itself (never resolved by id alone) so a citation can
    never be backed by another run's event, matching the same run-scoped discipline
    :func:`reads_for_path` already applies. The whole-payload size-cap placeholder
    (``{truncated: true, original_bytes, preview}`` - ``application/events.py``'s
    ``cap_payload``) is reported as ``placeholder=True`` with no content, never mistaken for
    an empty but genuine content field.
    """
    if not event_id:
        return None
    event = session.execute(
        select(Event.id, Event.type, Event.sequence, Event.payload).where(
            Event.id == event_id, Event.run_id == run_id
        )
    ).first()
    if event is None:
        return None
    _id, etype, sequence, payload = event
    payload = payload or {}
    ref = RetainedOutputRef.from_payload(payload.get(RETAINED_KEY))
    if artifact_store is not None and ref is not None and etype == EventType.TOOL_RESULT.value:
        retained = load_output(artifact_store, run_id, ref)
        if retained is not None:
            return EventView(
                event_id=_id,
                type=str(etype),
                sequence=sequence,
                content_key="output",
                content=retained,
                truncated=ref.truncated,
                placeholder=False,
                retained=True,
            )
    if payload.get("truncated") is True and "preview" in payload:
        return EventView(
            event_id=_id,
            type=str(etype),
            sequence=sequence,
            content_key=None,
            content=None,
            truncated=True,
            placeholder=True,
        )
    content_key: str | None = None
    content: str | None = None
    for key in _EVENT_CONTENT_KEYS:
        value = payload.get(key)
        if isinstance(value, str):
            content_key, content = key, value
            break
    field_truncated = bool(content_key and payload.get(f"{content_key}_truncated"))
    return EventView(
        event_id=_id,
        type=str(etype),
        sequence=sequence,
        content_key=content_key,
        content=content,
        truncated=field_truncated,
        placeholder=False,
    )
