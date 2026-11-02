"""The hook coverage facts of a run whose reads come only from Claude Code hooks (an
observed run, or a Herdr-hosted Claude Code session), read from its ``observed_sessions``
row(s).

Its own module so both File Evidence's completeness statement
(``file_evidence.store.completeness``) and citation verification
(``findings.evidence.run_window``) read the same facts without importing each other;
:func:`agenttape.findings.verify.observed_gaps` alone decides what they mean.
"""

from __future__ import annotations

from itertools import pairwise
from typing import Any

from sqlalchemy import ColumnElement, select
from sqlalchemy.orm import Session

from agenttape.domain.enums import RunKind
from agenttape.findings.verify import FRESH_SESSION_STARTS, ObservedCoverage
from agenttape.infrastructure.db.extra_models.observed_sessions import ObservedSession
from agenttape.infrastructure.db.models import Run

#: ``observed_sessions.details`` key for the id of the AgentTape process observing a
#: session (``application.services.observed_sessions`` writes it; see there).
OBSERVER_INSTANCE_KEY = "observer_instance"


def observed_coverage(session: Session, run: Run) -> ObservedCoverage | None:
    """What the ``observed_sessions`` row(s) of a run whose reads come only from Claude
    Code hooks record about that hook coverage: an observed run, or a launched Herdr run
    hosting a Claude Code session its hooks reached (``mode="hosted"``). ``None`` for any
    other run AgentTape launched (it owns that run's telemetry end to end; a Herdr run no
    hook ever reached has no read telemetry at all, so its reads are already unavailable).

    Facts only, never a judgement: :func:`agenttape.findings.verify.observed_gaps` decides.
    Every row counts, not just the first: an observed run normally has one, but a Herdr run
    hosts every Claude Code session its pane runs (a ``/clear``, a restarted ``claude``, a
    ``claude --resume``, or the same session after an idle close each add a row), and each
    must have started under observation (:func:`_from_rows`).
    """
    if run.kind == RunKind.OBSERVED.value:
        rows = _rows(session, run, ObservedSession.source == "hook")
        if run.source != "hook" or not rows:
            # An MCP-created run never had a hook; a hook-created run whose row is gone
            # has coverage nothing can show any more.
            return ObservedCoverage(
                source=str(run.source),
                recorded=run.source != "hook",
                first_hook=None,
                first_hook_source=None,
                dropped_hook_events=0,
                observer_restarts=0,
            )
        return _from_rows(rows)
    hosted = _rows(session, run, ObservedSession.mode == "hosted")
    return _from_rows(hosted) if hosted else None


def _rows(session: Session, run: Run, condition: ColumnElement[bool]) -> list[ObservedSession]:
    return list(
        session.scalars(
            select(ObservedSession)
            .where(ObservedSession.run_id == run.id, condition)
            .order_by(ObservedSession.first_seen_at, ObservedSession.id)
        )
    )


def _text(details: dict[str, Any], key: str) -> str | None:
    value = details.get(key)
    return value if isinstance(value, str) else None


def _from_rows(rows: list[ObservedSession]) -> ObservedCoverage:
    """The coverage facts of every row of one run, in the order the sessions began.

    * ``recorded`` only when every row recorded its first hook.
    * ``first_hook``/``first_hook_source`` are those of the first row whose session did not
      begin with a fresh ``SessionStart`` (the one a gap would name), else the first row's.
    * Dropped hook events and restarts are summed over every row, and a change of observing
      AgentTape process from one row to the next counts as a restart too: a row created by
      a new process after the previous row's process stopped carries no trace of that on
      either row alone, and hooks sent while no AgentTape was running were lost.
    """
    details = [dict(row.details or {}) for row in rows]
    joined = next(
        (
            d
            for d in details
            if _text(d, "first_hook") != "SessionStart"
            or _text(d, "first_hook_source") not in FRESH_SESSION_STARTS
        ),
        None,
    )
    named = joined if joined is not None else details[0]
    instances = [_text(d, OBSERVER_INSTANCE_KEY) for d in details]
    cross_row_restarts = sum(
        1
        for before, after in pairwise(instances)
        if before is not None and after is not None and before != after
    )
    return ObservedCoverage(
        source="hook",
        recorded=all(isinstance(d.get("first_hook"), str) for d in details),
        first_hook=_text(named, "first_hook"),
        first_hook_source=_text(named, "first_hook_source"),
        dropped_hook_events=sum(int(d.get("dropped_hook_events") or 0) for d in details),
        observer_restarts=sum(int(d.get("observer_restarts") or 0) for d in details)
        + cross_row_restarts,
        sessions=len(rows),
    )
