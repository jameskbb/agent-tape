"""Event persistence: sequence allocation, redaction, and size caps (ADR 0004).

:class:`EventRecorder` is the only code that inserts ``events`` rows. It joins the
caller's transaction so a status change and its lifecycle event commit together.

A recorder given an artifact store also keeps the redacted output of a read's
``tool_result`` that is over the event text cap, up to the retained-content cap, for
verification (``findings/retained_output.py``); the event row itself stays capped.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from agenttape.artifacts.store import ArtifactStore
from agenttape.config import Settings
from agenttape.domain.enums import EventType
from agenttape.domain.events import EVENT_SCHEMA_VERSION, EventDraft
from agenttape.file_evidence.ledger import is_read_call
from agenttape.findings.retained_output import RETAINED_KEY, store_output
from agenttape.infrastructure.clock import Clock
from agenttape.infrastructure.db.base import new_id
from agenttape.infrastructure.db.models import Event, Run
from agenttape.security.redaction import DEFAULT_REDACTOR, RedactionReport, Redactor

log = logging.getLogger(__name__)

MAX_SUMMARY_CHARS = 500
_TEXT_KEYS = ("text", "output", "content", "message", "diff", "body")
#: Small fixed-size keys a whole-payload placeholder keeps (the retained-output reference).
_KEPT_KEYS = (RETAINED_KEY,)
#: Hook ``response_shape`` values whose output can be a read's text (``capture/claude_hooks``).
_RETAINED_SHAPES = frozenset({"read_file", "bash"})


def _json_size(value: Any) -> int:
    return len(json.dumps(value, default=str, ensure_ascii=False).encode("utf-8"))


def cap_payload(payload: dict[str, Any], *, max_bytes: int, max_text: int) -> dict[str, Any]:
    """Trim long text fields, then fall back to a preview if still over ``max_bytes``."""
    out = dict(payload)
    for key in _TEXT_KEYS:
        value = out.get(key)
        if isinstance(value, str) and len(value.encode("utf-8")) > max_text:
            encoded = value.encode("utf-8")[:max_text]
            out[key] = encoded.decode("utf-8", errors="ignore")
            out[f"{key}_truncated"] = True
            out[f"{key}_original_bytes"] = len(value.encode("utf-8"))
    if _json_size(out) <= max_bytes:
        return out
    preview = json.dumps(out, default=str, ensure_ascii=False)[:max_text]
    kept = {key: out[key] for key in _KEPT_KEYS if key in out}
    return {"truncated": True, "original_bytes": _json_size(out), "preview": preview, **kept}


class EventRecorder:
    def __init__(
        self, settings: Settings, clock: Clock, *, artifact_store: ArtifactStore | None = None
    ) -> None:
        self._settings = settings
        self._clock = clock
        #: Where a read's over-cap output is kept for verification; ``None`` keeps nothing
        #: beyond the event itself (every recorder that never records tool results).
        self._artifact_store = artifact_store

    def _parent_payload(
        self, session: Session, run_id: str, parent_id: str, batch: Mapping[str, EventDraft]
    ) -> Mapping[str, Any] | None:
        """The payload of ``parent_id``: a draft in this batch, or an event of this run."""
        draft = batch.get(parent_id)
        if draft is not None:
            return draft.payload or {}
        parent = session.get(Event, parent_id)
        if parent is None or parent.run_id != run_id:
            return None
        return parent.payload or {}

    def _retain_read_output(
        self,
        session: Session,
        run_id: str,
        draft: EventDraft,
        payload: dict[str, Any],
        batch: Mapping[str, EventDraft],
    ) -> dict[str, Any] | None:
        """Keep ``payload``'s (already redacted) output for verification when it is a read's
        ``tool_result`` over the event text cap; the reference to store, or ``None``.

        A failed write is logged and never blocks the event: the read is then verified
        against the capped event text, exactly as before.
        """
        if self._artifact_store is None or EventType(draft.type) != EventType.TOOL_RESULT:
            return None
        output = payload.get("output")
        if not isinstance(output, str) or not draft.parent_event_id:
            return None
        # A hook result says what its output is: only a file's text (a Read) or a command's
        # stdout (Bash) can back a line citation. A "json" dump (an image or PDF Read, with
        # base64 content) or an error is never kept.
        shape = payload.get("response_shape")
        if shape is not None and shape not in _RETAINED_SHAPES:
            return None
        if len(output.encode("utf-8")) <= self._settings.max_event_text_bytes:
            return None
        parent = self._parent_payload(session, run_id, draft.parent_event_id, batch)
        if parent is None or not is_read_call(parent):
            return None
        try:
            ref = store_output(self._artifact_store, run_id, output)
        except Exception:  # disk full, over the artifact size limit: keep the event anyway
            log.warning("could not keep a read's full output for run %s", run_id, exc_info=True)
            return None
        # A result the adapter itself reports as cut upstream (the draft as submitted, before
        # this recorder's own caps add their flags) is never a possible whole file either.
        submitted = draft.payload or {}
        if submitted.get("truncated") or submitted.get("output_truncated"):
            ref = dataclasses.replace(ref, truncated=True)
        return ref.as_payload()

    def append(
        self,
        session: Session,
        run_id: str,
        drafts: Sequence[EventDraft],
        *,
        redactor: Redactor | None = None,
    ) -> list[Event]:
        """Persist ``drafts`` in order with consecutive per-run sequence numbers."""
        if not drafts:
            return []
        red = redactor or DEFAULT_REDACTOR
        n = len(drafts)
        # Atomic allocation: the UPDATE takes the run row's write lock until commit.
        session.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(event_seq=Run.event_seq + n)
            .execution_options(synchronize_session=False)
        )
        row = session.execute(select(Run.event_seq, Run.created_at).where(Run.id == run_id)).one()
        last_seq, run_created = int(row[0]), row[1]
        first_seq = last_seq - n + 1
        now = self._clock.now()
        batch = {draft.id: draft for draft in drafts if draft.id}
        events: list[Event] = []
        for offset, draft in enumerate(drafts):
            report = RedactionReport()
            summary = red.redact_text(str(draft.summary), report)[:MAX_SUMMARY_CHARS]
            payload = red.redact_value(draft.payload or {}, report)
            # Only this recorder names a retained copy; a draft's own value is never kept.
            payload.pop(RETAINED_KEY, None)
            retained = self._retain_read_output(session, run_id, draft, payload, batch)
            if retained is not None:
                payload[RETAINED_KEY] = retained
            payload = cap_payload(
                payload,
                max_bytes=self._settings.max_event_payload_bytes,
                max_text=self._settings.max_event_text_bytes,
            )
            mono = (
                float(draft.monotonic_ms)
                if draft.monotonic_ms is not None
                else max(0.0, (now - run_created).total_seconds() * 1000.0)
            )
            event = Event(
                id=draft.id or new_id(),
                run_id=run_id,
                attempt_id=draft.attempt_id,
                step_run_id=draft.step_run_id,
                node_key=draft.node_key,
                parent_event_id=draft.parent_event_id,
                sequence=first_seq + offset,
                ts=now,
                monotonic_ms=round(mono, 3),
                type=EventType(draft.type).value,
                source=draft.source,
                severity=str(draft.severity),
                summary=summary,
                payload=payload,
                redaction=report.as_metadata() if report.count else None,
                usage=red.redact_value(draft.usage) if draft.usage else None,
                cost=red.redact_value(draft.cost) if draft.cost else None,
                schema_version=EVENT_SCHEMA_VERSION,
            )
            session.add(event)
            events.append(event)
        session.flush()
        session.info.setdefault("agenttape_notify_runs", set()).add(run_id)
        return events

    def record(
        self,
        session: Session,
        run_id: str,
        type: EventType | str,
        summary: str,
        payload: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Event:
        draft = EventDraft(type=type, summary=summary, payload=payload or {}, **kwargs)
        return self.append(session, run_id, [draft])[0]
