"""Worker hooks for context evidence.

* attempt monitor - subscribes the normalizer to the attempt's shared evidence feed
  (:mod:`agenttape.worker.evidence_feed`, also read by File Evidence) and polls it about
  once a second, so context evidence is persisted continuously (not only at completion);
* run terminal    - one full, idempotent re-scan once the run ends or pauses for approval,
  which also covers events committed out of order and attempts ended by restart recovery;
* back-fill       - :func:`backfill_findings`, run by the worker when it starts: ingests the
  FINDING events of finished runs that have no ``findings`` row (runs recorded before the
  findings table existed).

The context pack itself is assembled by the agent executor before the adapter starts
(:func:`agenttape.context_evidence.pack.prepare_attempt_context`), because the approved
lessons must be in the prompt the adapter receives.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from agenttape.context_evidence.normalize import (
    RELEVANT_TYPES,
    normalize_events,
    normalize_run,
    processed_through,
)
from agenttape.domain.enums import TERMINAL_STATUS_VALUES, EventType
from agenttape.infrastructure.db.engine import begin_immediate
from agenttape.infrastructure.db.extra_models.findings import Finding
from agenttape.infrastructure.db.models import Event, Run, RunAttempt
from agenttape.worker import hooks
from agenttape.worker.evidence_feed import POLL_SECONDS, feed_for

if TYPE_CHECKING:
    from agenttape.adapters.base import EventEmitter, ExecutionContext
    from agenttape.application.context import AppContext

log = logging.getLogger(__name__)


def _last_workdir(session: Session, run_id: str) -> Path | None:
    last = session.scalars(
        select(RunAttempt.workdir)
        .where(RunAttempt.run_id == run_id)
        .order_by(RunAttempt.attempt_number.desc())
        .limit(1)
    ).first()
    return Path(last) if last else None


def rescan_run(ctx: AppContext, run_id: str) -> int:
    """Normalize every relevant event of the run, in the last attempt's directory."""
    with ctx.session() as session:
        begin_immediate(session)
        return normalize_run(session, ctx.clock, run_id, 0, _last_workdir(session, run_id))


def _unfound_finding_events() -> Select[tuple[Event]]:
    """FINDING events with no ``findings`` row (``origin_event_id``)."""
    return (
        select(Event)
        .outerjoin(Finding, Finding.origin_event_id == Event.id)
        .where(Event.type == EventType.FINDING.value, Finding.id.is_(None))
    )


def backfill_findings(ctx: AppContext) -> int:
    """Ingest every FINDING event of a finished run that has no ``findings`` row; returns
    how many were ingested. A database upgraded past ``m3F_0001`` has such events for every
    run recorded before the findings table existed, and a finished run is never re-scanned
    otherwise, so its findings would be missing from the Findings tab and inbox. Only the
    missing events go through :func:`normalize_events`, the same path every FINDING takes
    (``FindingsService.ingest_event`` is idempotent on ``origin_event_id`` and the legacy
    links it writes are de-duplicated), so running this again changes nothing. One run per
    transaction, so a large back-fill never holds the write lock for long."""
    with ctx.session() as session:
        # Driven from runs, so each probe uses the (run_id, type) events index.
        run_ids = list(
            session.scalars(
                select(Run.id).where(
                    Run.status.in_(TERMINAL_STATUS_VALUES),
                    _unfound_finding_events().where(Event.run_id == Run.id).exists(),
                )
            )
        )
    ingested = 0
    for run_id in run_ids:
        with ctx.session() as session:
            begin_immediate(session)
            run = session.get(Run, run_id)
            if run is None or run.status not in TERMINAL_STATUS_VALUES:
                continue
            events = list(
                session.scalars(
                    _unfound_finding_events().where(Event.run_id == run_id).order_by(Event.sequence)
                )
            )
            normalize_events(session, ctx.clock, run, events, _last_workdir(session, run_id))
            session.flush()
            ingested += int(
                session.scalar(
                    select(func.count())
                    .select_from(Finding)
                    .where(Finding.origin_event_id.in_([ev.id for ev in events]))
                )
                or 0
            )
    if ingested:
        log.info("back-filled %d findings recorded before the findings table", ingested)
    return ingested


async def context_monitor(
    ctx: AppContext, exec_ctx: ExecutionContext, _system: EventEmitter
) -> None:
    run_id, workdir = exec_ctx.run.id, exec_ctx.workdir
    feed = feed_for(ctx, run_id, exec_ctx.attempt.id)

    def cursor() -> int:
        with ctx.session() as session:
            return processed_through(session, run_id)

    def consume(session: Session, events: list[Event]) -> None:
        run = session.get(Run, run_id)
        if run is not None:
            normalize_events(session, ctx.clock, run, events, workdir)

    feed.subscribe(RELEVANT_TYPES, consume, after=await asyncio.to_thread(cursor))
    while True:
        try:
            await asyncio.to_thread(feed.poll)
        except Exception:  # keep observing; the terminal re-scan catches up
            log.exception("context evidence sync failed for run %s", run_id)
        await asyncio.sleep(POLL_SECONDS)


async def context_on_terminal(ctx: AppContext, run_id: str, _status: str) -> None:
    await asyncio.to_thread(rescan_run, ctx, run_id)


hooks.register_attempt_monitor(context_monitor)
hooks.register_on_run_terminal(context_on_terminal)
