"""``CaptureService``: writes one FINDING event per ``record_finding`` call.

This is the only write this service performs. It never inserts a ``findings``/``citations``
row itself - the worker's attempt monitor (and the terminal rescan) run the existing
normalizer, which calls ``FindingsService.ingest_event`` exactly as it does for every other
FINDING event source.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agenttape.application.events import EventRecorder
from agenttape.application.schemas.capture import FindingRecordIn, FindingRecordOut
from agenttape.application.services.observed_sessions import ObservedSessionService
from agenttape.domain.enums import EventType, RunKind, RunStatus
from agenttape.domain.events import EventDraft
from agenttape.infrastructure.db.engine import begin_immediate
from agenttape.infrastructure.db.extra_models.observed_sessions import ObservedSession
from agenttape.infrastructure.db.models import Event, Run, RunAttempt

if TYPE_CHECKING:
    from agenttape.application.context import AppContext


@dataclass
class InteractiveFindingOutcome:
    """The outcome of :meth:`CaptureService.record_interactive_finding`."""

    #: "recorded" | "ambiguous" | "outside_projects" | "observation_cancelled" |
    #: "session_mismatch"
    status: str
    result: FindingRecordOut | None = None
    #: A ready-to-return message for the router's non-2xx response body.
    message: str | None = None


#: A finding identical to one the same MCP server (``instance_id``) recorded on the same
#: attempt this recently is the same finding sent again (its request timed out, or
#: AgentTape answered "may have been recorded", and the agent retried): the earlier event
#: is returned instead of a copy. Longer than the MCP client's own timeout plus a retry.
FINDING_RETRY_WINDOW_SECONDS = 60


def finding_payload(record: FindingRecordIn) -> dict[str, Any]:
    """The exact event payload ``CaptureService.record_finding`` writes, so a caller (the
    router's size check) can measure it before any database access. ``capture_digest`` is
    a sha256 of everything else in it plus the sending MCP server's ``instance_id``, so a
    retried identical finding from that server is recognized, and the same words from
    another session never are."""
    links = [c.model_dump(exclude_none=True) for c in record.citations]
    payload: dict[str, Any] = {
        "kind": record.kind,
        "text": record.text,
        "links": links,
        "recorded_via": "mcp",
    }
    if record.target is not None:
        payload["target"] = record.target.model_dump()
    canonical = json.dumps(
        {"payload": payload, "instance_id": record.instance_id},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    payload["capture_digest"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return payload


class CaptureService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    def _write(
        self, session: Session, run_id: str, attempt_id: str, record: FindingRecordIn
    ) -> Event:
        """The one FINDING event write for both capture paths. Idempotent for a retry: a
        finding identical to one the same MCP server recorded on this attempt in the last
        :data:`FINDING_RETRY_WINDOW_SECONDS` returns that event rather than a copy (the
        caller holds a ``BEGIN IMMEDIATE`` transaction, so two retries cannot both miss)."""
        payload = finding_payload(record)
        since = self.ctx.clock.now() - timedelta(seconds=FINDING_RETRY_WINDOW_SECONDS)
        for earlier in session.scalars(
            select(Event).where(
                Event.run_id == run_id,
                Event.type == EventType.FINDING.value,
                Event.attempt_id == attempt_id,
                Event.source == "agenttape-mcp",
                Event.ts >= since,
            )
        ):
            if (earlier.payload or {}).get("capture_digest") == payload["capture_digest"]:
                return earlier
        # Redacted with the attempt's own secret values when its worker runs in this
        # process (security/attempt_redactors.py), else with AGENTTAPE_REDACT_ENV's values.
        redactor = self.ctx.attempt_redactors.for_attempt(attempt_id, self.ctx.settings.redact_env)
        draft = EventDraft(
            type=EventType.FINDING,
            summary=record.text[:500],
            payload=payload,
            source="agenttape-mcp",
            attempt_id=attempt_id,
        )
        return EventRecorder(self.ctx.settings, self.ctx.clock).append(
            session, run_id, [draft], redactor=redactor
        )[0]

    def record_finding(
        self, *, run_id: str, attempt_id: str, record: FindingRecordIn
    ) -> FindingRecordOut:
        with self.ctx.session() as session:
            begin_immediate(session)  # the retry check and the write are one step
            event = self._write(session, run_id, attempt_id, record)
            return FindingRecordOut(
                run_id=run_id, event_id=event.id, citations=len(record.citations)
            )

    def record_interactive_finding(self, record: FindingRecordIn) -> InteractiveFindingOutcome:
        """Resolve and record a finding from an interactive or Herdr-hosted session
        (``record.cwd``/``record.instance_id`` are required by the router before this is
        called). One transaction: resolve, write the FINDING event on the resolved
        attempt exactly as :meth:`record_finding` does, touch the row, and - for an
        ``observed`` run only - normalize the new event immediately (an attached/hosted
        run is normalized by its own worker monitor already).

        Runs on the hook ingest thread (``HookIngest.in_order``). Before the finding is
        written, the session's pending dropped hooks (refused or lost since its last
        ingested hook) are written onto its row, so a drop that happened before the
        finding is always recorded, and sequenced, before it."""
        observed = ObservedSessionService(self.ctx)
        taken: list[tuple[str, int]] = []
        try:
            return self._record_interactive(observed, record, taken)
        except BaseException:
            observed.restore_drops(taken)
            raise

    def _record_interactive(
        self,
        observed: ObservedSessionService,
        record: FindingRecordIn,
        taken: list[tuple[str, int]],
    ) -> InteractiveFindingOutcome:
        assert record.cwd is not None
        assert record.instance_id is not None
        with self.ctx.session() as session:
            begin_immediate(session)  # an observed run's finding is normalized inline below
            resolution = observed.resolve_for_mcp(
                session,
                cwd=record.cwd,
                instance_id=record.instance_id,
                session_id=record.session_id,
            )
            if resolution.status == "ambiguous":
                return InteractiveFindingOutcome(
                    status="ambiguous",
                    message=(
                        f"AgentTape sees {resolution.active_count} active Claude Code "
                        f"sessions in {resolution.cwd} and cannot tell which one is yours; "
                        "nothing was recorded. Pass session_id to record_finding."
                    ),
                )
            if resolution.status == "outside_projects":
                return InteractiveFindingOutcome(
                    status="outside_projects",
                    message=(
                        f"{record.cwd} is not inside a registered AgentTape project; "
                        "the finding was not recorded."
                    ),
                )
            if resolution.status == "observation_cancelled":
                return InteractiveFindingOutcome(
                    status="observation_cancelled",
                    message=(
                        "AgentTape stopped observing this session; the finding was not recorded."
                    ),
                )
            if resolution.status == "session_mismatch":
                return InteractiveFindingOutcome(
                    status="session_mismatch",
                    message=(
                        "The Claude Code session named by session_id is not one this MCP "
                        "server can record for (it is in another directory, is a run "
                        "AgentTape launched, or is already bound to another MCP server); "
                        "nothing was recorded."
                    ),
                )
            run_id = resolution.run_id
            attempt_id = resolution.attempt_id
            assert run_id is not None
            assert attempt_id is not None
            row = session.scalars(
                select(ObservedSession).where(
                    ObservedSession.session_hash == resolution.session_hash,
                    ObservedSession.status == "active",
                )
            ).first()
            if row is not None:
                observed.absorb_pending_drops(session, row, taken)

            event = self._write(session, run_id, attempt_id, record)
            if resolution.mode == "observed":
                observed.feed_observed_evidence(session, run_id, attempt_id, [event])
            return InteractiveFindingOutcome(
                status="recorded",
                result=FindingRecordOut(
                    run_id=run_id, event_id=event.id, citations=len(record.citations)
                ),
            )

    @staticmethod
    def verify_attempt(session: Session, attempt_id: str) -> tuple[str, str] | None:
        """``(run_id, attempt_id)`` when the attempt is running on a claude-code agent run."""
        row = session.execute(
            select(RunAttempt.run_id, RunAttempt.status, Run.kind, Run.adapter_type)
            .join(Run, Run.id == RunAttempt.run_id)
            .where(RunAttempt.id == attempt_id)
        ).first()
        if row is None:
            return None
        run_id, status, kind, adapter_type = row
        if (
            status != RunStatus.RUNNING.value
            or kind != RunKind.AGENT.value
            or adapter_type != "claude-code"
        ):
            return None
        return run_id, attempt_id
