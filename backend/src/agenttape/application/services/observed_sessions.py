"""``ObservedSessionService``: correlates a Claude Code ``http`` hook with a run.

``ingest_hook`` finds (or creates) the ``observed_sessions`` row for the hashed session id,
creates the ``observed`` run through :class:`RunService` (never a second run writer), and
records tool events through the existing ``EventRecorder`` - normalizing them into context
evidence immediately for ``mode == "observed"`` rows (no worker, no evidence-feed poll).

``SessionEnd`` and :meth:`sweep_idle` close the run honestly (``succeeded``/``closed_by
session_end`` or ``timed_out``/``idle_timeout``, never anything implying the agent
itself finished normally) and hand the caller the closed ``(run_id, status)`` pair so the
run-terminal hooks (the same ones an ordinary run's completion fires) run for observed
runs too.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agenttape.application.events import EventRecorder
from agenttape.application.services.ingest_state import on_ingest_thread, pending_drops
from agenttape.application.services.runs import RunService
from agenttape.application.transitions import (
    ConcurrentUpdateError,
    transition_attempt,
    transition_run,
)
from agenttape.capture.claude_hooks import HookEvent, hook_event_ids, session_hash, tool_drafts
from agenttape.context_evidence.normalize import RELEVANT_TYPES, normalize_events
from agenttape.domain.enums import (
    ACTIVE_STATUS_VALUES,
    TERMINAL_STATUS_VALUES,
    EventType,
    FailureCategory,
    RunKind,
    RunSource,
    RunStatus,
    Severity,
)
from agenttape.file_evidence.ledger import EventRow, telemetry_evidence
from agenttape.file_evidence.store import insert_evidence, rebuild_summaries
from agenttape.findings.coverage import OBSERVER_INSTANCE_KEY
from agenttape.herdr.sessions import LIVE_STATUSES
from agenttape.infrastructure.db.engine import begin_immediate
from agenttape.infrastructure.db.extra_models.observed_sessions import ObservedSession
from agenttape.infrastructure.db.extra_models.runtime_sessions import RuntimeSession
from agenttape.infrastructure.db.models import Event, Project, Run, RunAttempt
from agenttape.security.paths import is_within
from agenttape.security.ratelimit import RateLimiter

log = logging.getLogger(__name__)

#: A run/attempt an interactive session may attach to (never a second run writer).
ATTACHABLE_MODES = ("attached", "hosted")
#: A hooks-token holder minting a fresh session_id per SessionStart can otherwise mint an
#: unbounded number of observed runs; this bounds it per AppContext.
MAX_NEW_OBSERVED_SESSIONS_PER_MINUTE = 30
#: How long an MCP server instance keeps its claim on a session row after its last call.
#: ``instance_id`` is minted per ``agenttape mcp serve`` process, so a restarted server
#: (a ``/mcp`` reconnect, a crash, a resumed session) arrives with a new one; once the bound
#: instance has been quiet this long, another instance may take the row over. Until then a
#: second instance is refused with ``session_mismatch``. Two minutes is long enough
#: that a live server recording findings keeps its session, and short enough that a
#: restarted one is not locked out of its own session for long.
MCP_BINDING_LEASE_SECONDS = 120
#: Only the most recent handovers are kept on a row, so a row cannot grow without bound.
MAX_RECORDED_MCP_REBINDS = 20
#: An unattributed dropped hook (one AgentTape could not tie to a session) is also written
#: onto rows closed up to this long before it was noticed: Claude Code abandons a hook
#: after 3 seconds and moves on, so the session that sent it may already have ended.
UNATTRIBUTED_DROP_LOOKBACK_SECONDS = 60
# ``OBSERVER_INSTANCE_KEY`` (defined in ``findings.coverage``, which reads it back across
# every row of a run) is the ``ctx.extensions`` key and ``observed_sessions.details`` key
# for the id of the AgentTape process receiving a session's hooks. Claude Code's ``http``
# hooks fail silently while AgentTape is down, so the only trace a restart leaves is a
# later hook or MCP call arriving at a different process than the one before it; each
# such change counts in ``details["observer_restarts"]`` and marks the session's evidence
# window incomplete (a missing read there is ``read_not_observed``, never ``never_read``).

if TYPE_CHECKING:
    from agenttape.application.context import AppContext


_OBSERVER_INSTANCE_LOCK = threading.Lock()


def observer_instance(ctx: AppContext) -> str:
    """This process's observer id, minted once per :class:`AppContext` (one ``serve``
    process has exactly one), stored on ``ctx.extensions`` like :func:`_creation_limiter`.
    Minted under a lock, so two threads asking at once never get two ids (which would
    read as a restart)."""
    with _OBSERVER_INSTANCE_LOCK:
        value = ctx.extensions.get(OBSERVER_INSTANCE_KEY)
        if not isinstance(value, str):
            value = uuid.uuid4().hex
            ctx.extensions[OBSERVER_INSTANCE_KEY] = value
        return value


def first_hook_details(hook: HookEvent, instance: str) -> dict[str, Any]:
    """The hook-coverage facts a new row records from the session's first hook: its name,
    ``SessionStart``'s ``source`` (only for ``SessionStart``; any other hook's ``source``
    field means nothing), and the observing process
    (``agenttape.findings.verify.observed_gaps`` decides from these)."""
    return {
        "first_hook": hook.name,
        "first_hook_source": hook.start_source if hook.name == "SessionStart" else None,
        OBSERVER_INSTANCE_KEY: instance,
    }


def _creation_limiter(ctx: AppContext) -> RateLimiter:
    """The one :class:`RateLimiter` bounding new ``observed`` runs minted through the hooks
    endpoint for ``ctx``, stored on ``ctx.extensions`` rather than module-global: each test
    gets a fresh context (and so a fresh limiter), while the one ``serve`` process gets
    exactly one limiter for its whole lifetime. Capture callers are loopback-only, so a
    per-client key would be one key anyway - one shared ``"hooks"`` key is simplest."""
    limiter = ctx.extensions.get("observed_session_creation")
    if not isinstance(limiter, RateLimiter):
        limiter = RateLimiter(rate=MAX_NEW_OBSERVED_SESSIONS_PER_MINUTE, per_seconds=60)
        ctx.extensions["observed_session_creation"] = limiter
    return limiter


#: A live session's own hook activity routinely pauses; the closed rows below never claim
#: the underlying Claude Code session itself was affected.
IDLE_CLOSE_NOTE = "AgentTape stopped observing this session (the session itself was not touched)."


@dataclass
class HookOutcome:
    #: "recorded" | "ignored"
    status: str
    reason: str | None = None
    run_id: str | None = None
    #: Set when this hook closed the run - the caller (the hooks router) schedules the
    #: run-terminal hooks for ``(run_id, status)`` after the response is sent.
    closed_run: tuple[str, str] | None = None


@dataclass
class McpResolution:
    """The outcome of resolving an interactive/Herdr-hosted ``record_finding`` call to a
    run (:meth:`ObservedSessionService.resolve_for_mcp`)."""

    #: "resolved" | "ambiguous" | "outside_projects" | "observation_cancelled" |
    #: "session_mismatch"
    status: str
    run_id: str | None = None
    attempt_id: str | None = None
    #: "observed" | "attached" | "hosted" - set only when status == "resolved"
    mode: str | None = None
    #: Set only when status == "ambiguous" - how many active sessions matched, and the
    #: directory they matched on (for the caller's error message).
    active_count: int | None = None
    cwd: str | None = None
    #: The resolved row's ``session_hash`` (set only when status == "resolved"), so the
    #: caller can record the session's pending dropped hooks before its finding.
    session_hash: str | None = None


def _contains(root: str, cwd: str) -> bool:
    """True when ``cwd`` equals or sits inside ``root``, both resolved (symlinks and
    ``..`` segments followed). Fails closed: when either path cannot be resolved (a NUL
    byte, a symlink loop, an OS error) the answer is ``False``, never a lexical guess,
    since ``/repo/../../etc`` is lexically "inside" ``/repo``.
    ``security/paths.is_within`` is ``relative_to``-based, so a root of ``/`` is handled
    correctly."""
    try:
        return is_within(Path(cwd).resolve(), Path(root).resolve())
    except (OSError, ValueError, RuntimeError):
        return False


def _mcp_lease_held_by_other(row: ObservedSession, instance_id: str, now: datetime) -> bool:
    """True when ``row`` is bound to an MCP server instance other than ``instance_id`` that
    has called within :data:`MCP_BINDING_LEASE_SECONDS` (``details["mcp_last_call_at"]``,
    renewed by every call that resolves to the row). A row bound before that timestamp was
    recorded has no provable live lease and counts as lapsed."""
    if row.mcp_instance is None or row.mcp_instance == instance_id:
        return False
    last = (row.details or {}).get("mcp_last_call_at")
    if not isinstance(last, str):
        return False
    try:
        last_at = datetime.fromisoformat(last)
    except ValueError:
        return False
    return now - last_at < timedelta(seconds=MCP_BINDING_LEASE_SECONDS)


def _is_file_inside(root: Path, rel: str) -> bool:
    """True when ``rel``, joined onto ``root``, is an existing regular file - refusing an
    absolute path (POSIX ``/...`` or a Windows drive form) or any ``..`` segment before
    ever stating anything, since ``root / rel`` replaces or escapes ``root`` for either."""
    if rel.startswith("/") or (len(rel) >= 2 and rel[1] == ":"):
        return False
    if ".." in PurePosixPath(rel).parts:
        return False
    return (root / rel).is_file()


def project_for_cwd(session: Session, cwd: str) -> Project | None:
    """The longest-matching non-archived project whose ``repo_path`` equals or contains
    ``cwd``. This resolves symlinks on disk (existence lookups of path components only,
    via ``Path.resolve()``); it never reads file content."""
    best: Project | None = None
    best_len = -1
    for project in session.scalars(select(Project).where(Project.archived.is_(False))):
        repo = project.repo_path
        if repo and _contains(repo, cwd) and len(repo) > best_len:
            best, best_len = project, len(repo)
    return best


class ObservedSessionService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    def _active_row(self, session: Session, hashed: str) -> ObservedSession | None:
        return session.scalars(
            select(ObservedSession).where(
                ObservedSession.session_hash == hashed, ObservedSession.status == "active"
            )
        ).first()

    def _last_closed_row(self, session: Session, hashed: str) -> ObservedSession | None:
        return session.scalars(
            select(ObservedSession)
            .where(ObservedSession.session_hash == hashed, ObservedSession.status == "closed")
            .order_by(ObservedSession.closed_at.desc())
        ).first()

    def _attached_candidate(
        self, session: Session, claimed_run_id: str | None, cwd: str | None
    ) -> tuple[Run, RunAttempt] | None:
        """The run ``claimed_run_id`` names, when it is an active, agent-kind,
        claude-code run with a running attempt whose ``workdir`` equals or contains
        ``cwd`` - a claim plus a matching working directory, never a claim alone (a header
        naming a finished, demo, or workflow run is ignored, as is a header whose cwd is
        outside the attempt's own workdir)."""
        if not claimed_run_id:
            return None
        run = session.get(Run, claimed_run_id)
        if (
            run is None
            or run.kind != RunKind.AGENT.value
            or run.adapter_type != "claude-code"
            or run.status not in ACTIVE_STATUS_VALUES
        ):
            return None
        attempt = session.scalars(
            select(RunAttempt).where(
                RunAttempt.run_id == run.id, RunAttempt.status == RunStatus.RUNNING.value
            )
        ).first()
        if attempt is None or not attempt.workdir or not cwd or not _contains(attempt.workdir, cwd):
            return None
        return run, attempt

    def _hosted_candidate(self, session: Session, cwd: str | None) -> tuple[Run, RunAttempt] | None:
        """Exactly one active Herdr runtime session (agent_kind ``claude``) whose running
        attempt's workdir equals or contains ``cwd`` - a header can never select this path
        (only the workdir match can); two matches is ambiguous (falls to
        observed)."""
        if not cwd:
            return None
        stmt = (
            select(RuntimeSession, RunAttempt, Run)
            .join(RunAttempt, RunAttempt.id == RuntimeSession.attempt_id)
            .join(Run, Run.id == RunAttempt.run_id)
            .where(
                RuntimeSession.status.in_(LIVE_STATUSES),
                RuntimeSession.agent_kind == "claude",
                RunAttempt.status == RunStatus.RUNNING.value,
                RunAttempt.workdir.is_not(None),
            )
        )
        candidates: list[tuple[Run, RunAttempt]] = []
        for _runtime_session, attempt, run in session.execute(stmt):
            # _contains fails closed, so a workdir that no longer resolves never matches.
            if _contains(str(attempt.workdir), cwd):
                candidates.append((run, attempt))
        if len(candidates) != 1:
            return None
        return candidates[0]

    def _compatible(self, session: Session, row: ObservedSession, cwd: str) -> bool:
        """True when an MCP caller at ``cwd`` may resolve to ``row``.

        An ``observed`` row's own ``cwd`` was itself proven inside a registered project
        when the row was created, so containment is transitive and an out-of-project
        caller can never match. A ``hosted`` row is compatible when its running attempt's
        ``workdir`` is set and contains ``cwd`` - the same rule :meth:`_hosted_candidate`
        applies to hooks, so the MCP caller's cwd is held to the same standard. An
        ``attached`` row is never compatible: a launched run's attempt accepts findings
        only through its own per-attempt token (``X-AgentTape-Attempt``), never through the
        per-machine ``mcp`` token."""
        if row.mode == "attached":
            return False
        if row.mode == "hosted":
            attempt = session.get(RunAttempt, row.attempt_id) if row.attempt_id else None
            return (
                attempt is not None
                and bool(attempt.workdir)
                and _contains(str(attempt.workdir), cwd)
            )
        return row.cwd is not None and _contains(row.cwd, cwd)

    def _resolve_new_session(
        self, session: Session, hook: HookEvent
    ) -> tuple[str, Run, RunAttempt] | None:
        """The run/attempt this session's first hook attaches to - ``attached`` (AgentTape
        launched it), ``hosted`` (AgentTape hosts it in Herdr) - or ``None`` when neither
        is unambiguous, in which case the caller falls back to an ``observed`` run."""
        claimed = self._attached_candidate(session, hook.claimed_run_id, hook.cwd)
        if claimed is not None:
            run, attempt = claimed
            return "attached", run, attempt
        hosted = self._hosted_candidate(session, hook.cwd)
        if hosted is not None:
            run, attempt = hosted
            return "hosted", run, attempt
        return None

    def resolve_for_mcp(
        self, session: Session, *, cwd: str, instance_id: str, session_id: str | None
    ) -> McpResolution:
        """Resolve which run an interactive/Herdr-hosted ``record_finding`` call belongs to.
        Resolution order:

        1. An explicit ``session_id`` naming an *active* row this caller is compatible with
           (:meth:`_compatible`: the caller's ``cwd`` is inside the row's own directory for
           an observed row, or inside the attempt's workdir for a hosted row; an attached
           row is never compatible) and that is unbound, already bound to this
           instance, or bound to an instance whose lease has lapsed: use it. Anything else
           the ``session_id`` could name (a row in another directory, an attached row, or a
           row another instance holds a live lease on) is refused
           with ``session_mismatch`` and nothing is written; the caller named a session
           explicitly, so this never falls through to steps 2-4. When the session has no
           active row but its most recent closed row was closed for ``cancelled``, refuse
           with ``observation_cancelled`` instead (the caller stopped observing that session
           for good; mirrors :meth:`ingest_hook`).
        2. An active row already bound to this MCP ``instance_id`` (``mcp_instance``) that
           this caller remains compatible with - a fast path so a second call from the same
           server process usually lands on the same run without re-matching cwd.
        3. Exactly one active hook-sourced row this caller is compatible with and that no
           other instance holds a live lease on: bind it (``mcp_instance = instance_id``;
           :meth:`_bind_for_mcp` takes a row over from another instance only once that
           instance's lease has lapsed). More than one such row is ambiguous.
        4. Otherwise, a project for ``cwd`` (none -> ``outside_projects``), else a brand-new
           ``observed`` run with source ``mcp``, owned by this instance from the start.

        A binding is a lease (:data:`MCP_BINDING_LEASE_SECONDS`), renewed by every call from
        the bound instance: while it is live no other instance can reach the row, and a
        row created by another MCP instance is never shared (step 3 considers only
        hook-sourced rows). A
        launched run's attached row is never resolved here at all - only its per-attempt
        token can write to it.

        Runs only on the hook ingest thread (``CaptureService.record_interactive_finding``
        through ``HookIngest.in_order``, asserted here): the finding it resolves for must
        be sequenced after every hook already received, and the observer bookkeeping it
        shares with ``ingest_hook`` (:meth:`_note_observer`) relies on the two never
        running at once.
        """
        assert on_ingest_thread(), "resolve_for_mcp runs only on the hook ingest thread"
        now = self.ctx.clock.now()
        if session_id:
            hashed = session_hash(session_id)
            row = self._active_row(session, hashed)
            if row is not None:
                if not self._compatible(session, row, cwd):
                    return McpResolution(status="session_mismatch")
                return self._bind_for_mcp(session, row, instance_id, now, resolved_by="session_id")
            last_closed = self._last_closed_row(session, hashed)
            if last_closed is not None and last_closed.close_reason == "cancelled":
                return McpResolution(status="observation_cancelled")

        bound = session.scalars(
            select(ObservedSession).where(
                ObservedSession.status == "active",
                ObservedSession.mcp_instance == instance_id,
                ObservedSession.mode != "attached",
            )
        ).first()
        if bound is not None and self._compatible(session, bound, cwd):
            return self._bind_for_mcp(session, bound, instance_id, now, resolved_by="mcp_instance")

        candidates: list[ObservedSession] = []
        for row in session.scalars(
            select(ObservedSession).where(
                ObservedSession.status == "active",
                ObservedSession.source == "hook",
                ObservedSession.mode != "attached",
            )
        ):
            if _mcp_lease_held_by_other(row, instance_id, now):
                continue
            if self._compatible(session, row, cwd):
                candidates.append(row)
        if len(candidates) == 1:
            return self._bind_for_mcp(
                session, candidates[0], instance_id, now, resolved_by="cwd_sole_candidate"
            )
        if len(candidates) > 1:
            return McpResolution(status="ambiguous", active_count=len(candidates), cwd=cwd)

        project = project_for_cwd(session, cwd)
        if project is None:
            return McpResolution(status="outside_projects")
        run, attempt = RunService(self.ctx).create_observed_run(
            session,
            project=project,
            cwd=cwd,
            source=RunSource.MCP,
            source_ref=f"mcp-instance:{instance_id[:12]}",
        )
        row = ObservedSession(
            session_hash=session_hash(f"mcp:{instance_id}"),
            run_id=run.id,
            attempt_id=attempt.id,
            project_id=project.id,
            mode="observed",
            source="mcp",
            cwd=cwd,
            status="active",
            first_seen_at=now,
            last_seen_at=now,
            mcp_instance=instance_id,
            details={"hook_events": {}, "mcp_last_call_at": now.isoformat()},
        )
        session.add(row)
        session.flush()
        return McpResolution(
            status="resolved",
            run_id=run.id,
            attempt_id=attempt.id,
            mode="observed",
            session_hash=row.session_hash,
        )

    def _bind_for_mcp(
        self,
        session: Session,
        row: ObservedSession,
        instance_id: str,
        now: datetime,
        *,
        resolved_by: str,
    ) -> McpResolution:
        """Bind ``row`` to ``instance_id`` as a lease: every caller reaching this method
        already passed a compatibility check, but a row another instance holds a live lease
        on (it called within :data:`MCP_BINDING_LEASE_SECONDS`) is refused rather than
        overwritten. Once that lease lapses the row is taken over, and the
        handover is appended to ``details["mcp_rebinds"]``. Every bind renews the
        lease (``details["mcp_last_call_at"]``). A new binding also records how the caller
        reached this row (``"session_id"``, ``"mcp_instance"``, or
        ``"cwd_sole_candidate"``) in ``details["mcp_resolved_by"]``, so a cwd-only match is
        visibly a heuristic, not a confirmation; a repeat call
        from the bound instance never overwrites it."""
        if _mcp_lease_held_by_other(row, instance_id, now):
            return McpResolution(status="session_mismatch")
        details = dict(row.details or {})
        if row.mcp_instance != instance_id:
            details["mcp_resolved_by"] = resolved_by
            if row.mcp_instance is not None:
                rebinds = list(details.get("mcp_rebinds") or [])
                rebinds.append({"from": row.mcp_instance, "at": now.isoformat()})
                details["mcp_rebinds"] = rebinds[-MAX_RECORDED_MCP_REBINDS:]
        details["mcp_last_call_at"] = now.isoformat()
        row.details = details
        row.mcp_instance = instance_id
        row.last_seen_at = now
        self._note_observer(session, row)
        session.flush()
        return McpResolution(
            status="resolved",
            run_id=row.run_id,
            attempt_id=row.attempt_id,
            mode=row.mode,
            session_hash=row.session_hash,
        )

    def _note_observer(self, session: Session, row: ObservedSession) -> None:
        """Record that this process is now observing ``row``'s session (a read-modify-write
        of ``row.details``: its callers, ``ingest_hook`` and :meth:`resolve_for_mcp`, run
        one at a time on the hook ingest thread); when a different
        process observed it before (AgentTape restarted mid-session, so any hook sent
        while it was down was lost without a trace), count the restart and record a
        WARNING. Observed and hosted rows only (both get their reads only from these
        hooks); an attached run's evidence window is its launched run's own. A row from
        before this was tracked is left alone: it already has no ``first_hook``, which
        marks its coverage unknown."""
        if row.mode not in ("observed", "hosted"):
            return
        current = observer_instance(self.ctx)
        details = dict(row.details or {})
        previous = details.get(OBSERVER_INSTANCE_KEY)
        if not isinstance(previous, str) or previous == current:
            return
        details[OBSERVER_INSTANCE_KEY] = current
        details["observer_restarts"] = int(details.get("observer_restarts", 0)) + 1
        row.details = details
        EventRecorder(self.ctx.settings, self.ctx.clock).record(
            session,
            row.run_id,
            EventType.LIFECYCLE,
            "AgentTape restarted while observing this Claude Code session; hooks sent "
            "while it was down were not recorded, so this session's evidence window is "
            "incomplete",
            {"phase": "observer_restarted"},
            severity=Severity.WARNING,
            attempt_id=row.attempt_id,
        )

    def record_dropped(
        self, session: Session, row: ObservedSession, dropped: int, *, unattributed: bool = False
    ) -> None:
        """Add ``dropped`` hooks to ``row``'s ``details["dropped_hook_events"]`` and record
        a WARNING event saying its evidence window is incomplete (nothing when 0)."""
        if dropped <= 0:
            return
        details = dict(row.details or {})
        details["dropped_hook_events"] = int(details.get("dropped_hook_events", 0)) + dropped
        row.details = details
        plural = "event was" if dropped == 1 else "events were"
        summary = (
            f"{dropped} Claude Code hook {plural} not recorded and could not be tied to one "
            "session, so every session AgentTape was observing counts it; this session's "
            "evidence window is incomplete"
            if unattributed
            else f"{dropped} Claude Code hook {plural} not recorded (AgentTape was "
            "saturated, rate-limited, or could not write it); this session's evidence "
            "window is incomplete"
        )
        payload: dict[str, Any] = {"phase": "hooks_dropped", "dropped": dropped}
        if unattributed:
            payload["unattributed"] = True
        EventRecorder(self.ctx.settings, self.ctx.clock).record(
            session,
            row.run_id,
            EventType.LIFECYCLE,
            summary,
            payload,
            severity=Severity.WARNING,
            attempt_id=row.attempt_id,
        )

    def absorb_pending_drops(
        self, session: Session, row: ObservedSession, taken: list[tuple[str, int]]
    ) -> None:
        """Write the hooks still pending for ``row``'s session
        (:class:`~agenttape.application.services.ingest_state.PendingDrops`) onto the
        row, appending what was taken to ``taken`` so the caller can put it back
        (:meth:`restore_drops`) when its transaction does not commit."""
        if row.source != "hook":
            return
        count = pending_drops(self.ctx).take(row.session_hash)
        if count > 0:
            taken.append((row.session_hash, count))
            self.record_dropped(session, row, count)

    def restore_drops(self, taken: list[tuple[str, int]]) -> None:
        drops = pending_drops(self.ctx)
        for hashed, count in taken:
            drops.mark(hashed, count)

    def record_unattributed_drops(self) -> None:
        """Write every unattributed pending drop onto each hook-sourced observed or hosted
        row open when it happened: every active row, and every row closed since
        :data:`UNATTRIBUTED_DROP_LOOKBACK_SECONDS` before the earliest one (a hook can be
        lost a while before AgentTape notices). One transaction; on failure the count is
        put back for the next attempt."""
        drops = pending_drops(self.ctx)
        count, since = drops.take_unattributed()
        if count <= 0:
            return
        try:
            with self.ctx.session() as session:
                begin_immediate(session)
                now = self.ctx.clock.now()
                cutoff = (since or now) - timedelta(seconds=UNATTRIBUTED_DROP_LOOKBACK_SECONDS)
                rows = session.scalars(
                    select(ObservedSession).where(
                        ObservedSession.source == "hook",
                        ObservedSession.mode.in_(("observed", "hosted")),
                        (ObservedSession.status == "active")
                        | (ObservedSession.closed_at >= cutoff),
                    )
                ).all()
                for row in rows:
                    self.record_dropped(session, row, count, unattributed=True)
        except Exception:
            drops.mark_unattributed(count, at=since)
            raise

    @staticmethod
    def close_rows_for_run(session: Session, run_id: str, *, reason: str, now: datetime) -> None:
        """Close every active ``observed_sessions`` row for ``run_id`` (a cancelled run
        stops being observed for good: :meth:`ingest_hook` refuses to reopen a session
        whose most recent row closed for this reason). Observed-mode rows also get their
        File Evidence summaries rebuilt so the ended run's evidence is complete."""
        rows = session.scalars(
            select(ObservedSession).where(
                ObservedSession.run_id == run_id, ObservedSession.status == "active"
            )
        ).all()
        rebuild = False
        for row in rows:
            row.status = "closed"
            row.closed_at = now
            row.close_reason = reason
            rebuild = rebuild or row.mode == "observed"
        if rebuild:
            rebuild_summaries(session, run_id, now=now)

    def ingest_hook(self, hook: HookEvent, *, dropped: int = 0) -> HookOutcome:
        hashed = session_hash(hook.session_id)
        with self.ctx.session() as session:
            begin_immediate(session)  # observed tool events are normalized inline below
            row = self._active_row(session, hashed)
            now = self.ctx.clock.now()
            if row is None:
                if hook.name == "SessionEnd":
                    return HookOutcome(status="ignored", reason="unknown_session")
                last_closed = self._last_closed_row(session, hashed)
                if last_closed is not None and last_closed.close_reason == "cancelled":
                    return HookOutcome(status="ignored", reason="observation_cancelled")
                resolved = self._resolve_new_session(session, hook)
                if resolved is not None:
                    mode, run, attempt = resolved
                    #: An attached row required a claimed run_id AND a matching workdir; a
                    #: hosted row is bound by workdir match alone - a heuristic, not a
                    #: confirmation.
                    resolved_by = "run_id_header" if mode == "attached" else "cwd_sole_candidate"
                    #: A hosted run's reads come only from these hooks, so it records the
                    #: same coverage facts an observed row does; an attached run's reads
                    #: come from its own stream-json telemetry.
                    coverage = (
                        first_hook_details(hook, observer_instance(self.ctx))
                        if mode == "hosted"
                        else {}
                    )
                    row = ObservedSession(
                        session_hash=hashed,
                        run_id=run.id,
                        attempt_id=attempt.id,
                        project_id=run.project_id,
                        mode=mode,
                        source="hook",
                        cwd=hook.cwd,
                        status="active",
                        first_seen_at=now,
                        last_seen_at=now,
                        details={"hook_events": {}, "resolved_by": resolved_by, **coverage},
                    )
                else:
                    project = project_for_cwd(session, hook.cwd) if hook.cwd else None
                    if project is None:
                        return HookOutcome(status="ignored", reason="outside_projects")
                    if not _creation_limiter(self.ctx).allow("hooks"):
                        log.warning(
                            "observed-run creation rate limited for session %s",
                            hashed[7:19],
                        )
                        return HookOutcome(status="ignored", reason="session_rate_limited")
                    run, attempt = RunService(self.ctx).create_observed_run(
                        session,
                        project=project,
                        cwd=hook.cwd or "",
                        source=RunSource.HOOK,
                        source_ref=f"claude-session:{hashed[7:19]}",
                        start_source=hook.start_source,
                        model=hook.model,
                    )
                    row = ObservedSession(
                        session_hash=hashed,
                        run_id=run.id,
                        attempt_id=attempt.id,
                        project_id=project.id,
                        mode="observed",
                        source="hook",
                        cwd=hook.cwd,
                        status="active",
                        first_seen_at=now,
                        last_seen_at=now,
                        details={
                            "hook_events": {},
                            **first_hook_details(hook, observer_instance(self.ctx)),
                        },
                    )
                session.add(row)
                session.flush()

            details = dict(row.details or {})
            counts = dict(details.get("hook_events") or {})
            counts[hook.name] = int(counts.get(hook.name, 0)) + 1
            details["hook_events"] = counts
            row.last_seen_at = now
            row.details = details
            run_id, attempt_id, mode = row.run_id, row.attempt_id, row.mode
            self._note_observer(session, row)
            self.record_dropped(session, row, dropped)

            if hook.name == "SessionEnd":
                closed_run: tuple[str, str] | None = None
                if mode == "observed":
                    current_run = session.get(Run, run_id)
                    current_attempt = session.get(RunAttempt, attempt_id) if attempt_id else None
                    if current_run is not None and current_run.status == RunStatus.RUNNING.value:
                        self._close_observed(
                            session,
                            row,
                            run=current_run,
                            attempt=current_attempt,
                            status=RunStatus.SUCCEEDED,
                            close_reason="session_end",
                            now=now,
                            end_reason=hook.end_reason,
                        )
                        closed_run = (run_id, RunStatus.SUCCEEDED.value)
                else:
                    row.status = "closed"
                    row.closed_at = now
                    row.close_reason = "session_end"
                session.flush()
                return HookOutcome(status="recorded", run_id=run_id, closed_run=closed_run)

            if hook.tool_name and hook.tool_use_id and attempt_id:
                if mode == "attached":
                    details = dict(row.details or {})
                    details["suppressed_tool_hooks"] = (
                        int(details.get("suppressed_tool_hooks", 0)) + 1
                    )
                    row.details = details
                else:
                    call_id, result_id = hook_event_ids(run_id, hook.tool_use_id)
                    call_exists = session.get(Event, call_id) is not None
                    result_exists = session.get(Event, result_id) is not None
                    already = (hook.name == "PreToolUse" and call_exists) or (
                        hook.name in ("PostToolUse", "PostToolUseFailure") and result_exists
                    )
                    if not already:
                        drafts = tool_drafts(
                            hook, run_id=run_id, attempt_id=attempt_id, call_exists=call_exists
                        )
                        # A hosted attempt's hooks can carry the secret values its prompt
                        # resolved: redact with that attempt's own redactor when its worker
                        # runs in this process, else with AGENTTAPE_REDACT_ENV's values.
                        redactor = self.ctx.attempt_redactors.for_attempt(
                            attempt_id, self.ctx.settings.redact_env
                        )
                        events = EventRecorder(
                            self.ctx.settings,
                            self.ctx.clock,
                            artifact_store=self.ctx.artifact_store,
                        ).append(session, run_id, drafts, redactor=redactor)
                        if mode == "observed":
                            self.feed_observed_evidence(session, run_id, attempt_id, events)
            session.flush()
            return HookOutcome(status="recorded", run_id=run_id)

    def feed_observed_evidence(
        self, session: Session, run_id: str, attempt_id: str, events: list[Event]
    ) -> None:
        """Observed-mode only: normalize tool events into Context Evidence immediately (no
        worker), and feed the same ``tool_call`` events into File Evidence."""
        relevant = [e for e in events if e.type in RELEVANT_TYPES]
        fresh_run = session.get(Run, run_id)
        fresh_attempt = session.get(RunAttempt, attempt_id)
        workdir = (
            Path(fresh_attempt.workdir)
            if fresh_attempt is not None and fresh_attempt.workdir
            else None
        )
        if relevant and fresh_run is not None:
            normalize_events(session, self.ctx.clock, fresh_run, relevant, workdir)
        if workdir is None:
            return
        root = workdir

        drafts = [
            draft
            for e in events
            if e.type == EventType.TOOL_CALL.value
            for draft in telemetry_evidence(
                EventRow(
                    id=e.id,
                    sequence=e.sequence,
                    ts=e.ts,
                    type=e.type,
                    source=e.source,
                    payload=e.payload or {},
                    attempt_id=e.attempt_id,
                    node_key=e.node_key,
                ),
                workdir,
                is_file=lambda rel: _is_file_inside(root, rel),
                runtime="hooks",
            )
        ]
        if drafts:
            insert_evidence(session, run_id, drafts)

    def _close_observed(
        self,
        session: Session,
        row: ObservedSession,
        *,
        run: Run,
        attempt: RunAttempt | None,
        status: RunStatus,
        close_reason: str,
        now: datetime,
        end_reason: str | None = None,
        message: str | None = None,
    ) -> None:
        """Transition the attempt then the run to ``status``, record honest outputs and a
        lifecycle event, and close the ``observed_sessions`` row. One conditional
        transition each (``transition_run``/``transition_attempt``) so a concurrent close
        (another sweeper, or a racing ``SessionEnd``) raises :class:`ConcurrentUpdateError`
        instead of double-closing."""
        start = run.started_at or run.queued_at or run.created_at
        duration_ms = int((now - start).total_seconds() * 1000) if start else None
        outputs: dict[str, Any] = {"closed_by": close_reason}
        if end_reason is not None:
            outputs["end_reason"] = end_reason
        run_fields: dict[str, Any] = {
            "finished_at": now,
            "duration_ms": duration_ms,
            "outputs": outputs,
        }
        attempt_fields: dict[str, Any] = {"finished_at": now}
        if status == RunStatus.TIMED_OUT:
            run_fields["failure_category"] = FailureCategory.IDLE_TIMEOUT.value
            run_fields["failure_message"] = message
            attempt_fields["failure_category"] = FailureCategory.IDLE_TIMEOUT.value
            attempt_fields["failure_message"] = message
        if attempt is not None:
            transition_attempt(session, attempt, status, **attempt_fields)
        transition_run(session, run, status, **run_fields)
        summary = (
            "Claude Code session ended"
            if close_reason == "session_end"
            else "AgentTape stopped observing this session"
        )
        EventRecorder(self.ctx.settings, self.ctx.clock).record(
            session,
            run.id,
            EventType.LIFECYCLE,
            summary,
            {
                "phase": "observed_ended",
                "status": status.value,
                "closed_by": close_reason,
                "end_reason": end_reason,
            },
            attempt_id=attempt.id if attempt is not None else None,
        )
        row.status = "closed"
        row.closed_at = now
        row.close_reason = close_reason
        rebuild_summaries(session, run.id, now=now)

    def sweep_idle(self) -> list[tuple[str, str]]:
        """Close every ``observed``-mode row whose ``last_seen_at`` is older than
        ``observed_idle_timeout_seconds`` (transitioning its run to ``timed_out``). Each
        row is its own transaction, so one :class:`ConcurrentUpdateError` (another
        sweeper, or a racing ``SessionEnd``) skips only that row.

        Also closes ``attached``/``hosted`` rows that have gone idle, or whose run
        already reached a terminal status on its own path - never transitioning the run
        or attempt (hooks never change a launched or hosted run's status)."""
        window = self.ctx.settings.observed_idle_timeout_seconds
        cutoff = self.ctx.clock.now() - timedelta(seconds=window)
        taken: list[tuple[str, int]] = []
        try:
            with self.ctx.session() as session:
                begin_immediate(session)  # closing a row writes its pending drops onto it
                stale_run_ids = list(
                    session.scalars(
                        select(ObservedSession.run_id).where(
                            ObservedSession.status == "active",
                            ObservedSession.mode == "observed",
                            ObservedSession.last_seen_at < cutoff,
                        )
                    )
                )
                self._close_attached_hosted_rows(session, cutoff, taken)
        except BaseException:
            self.restore_drops(taken)
            raise
        minutes = max(1, int(window // 60))
        message = f"No Claude Code hook activity for {minutes} minutes; {IDLE_CLOSE_NOTE}"
        closed: list[tuple[str, str]] = []
        for run_id in stale_run_ids:
            result = self._close_idle_run(run_id, message)
            if result is not None:
                closed.append(result)
        return closed

    def _close_attached_hosted_rows(
        self, session: Session, cutoff: datetime, taken: list[tuple[str, int]]
    ) -> None:
        now = self.ctx.clock.now()
        rows = session.execute(
            select(ObservedSession, Run.status)
            .join(Run, Run.id == ObservedSession.run_id)
            .where(
                ObservedSession.status == "active",
                ObservedSession.mode.in_(ATTACHABLE_MODES),
            )
        ).all()
        for row, run_status in rows:
            if run_status in TERMINAL_STATUS_VALUES:
                reason = "run_ended"
            elif row.last_seen_at < cutoff:
                reason = "idle_window"
            else:
                continue
            # Nothing else will ever write this session's pending drops once it is closed.
            self.absorb_pending_drops(session, row, taken)
            row.status = "closed"
            row.closed_at = now
            row.close_reason = reason
        session.flush()

    def _close_idle_run(self, run_id: str, message: str) -> tuple[str, str] | None:
        now = self.ctx.clock.now()
        taken: list[tuple[str, int]] = []
        try:
            with self.ctx.session() as session:
                begin_immediate(session)  # the row's pending drops are written below
                row = session.scalars(
                    select(ObservedSession).where(
                        ObservedSession.run_id == run_id, ObservedSession.status == "active"
                    )
                ).first()
                if row is None:
                    return None
                run = session.get(Run, row.run_id)
                if run is None or run.status != RunStatus.RUNNING.value:
                    return None
                attempt = session.get(RunAttempt, row.attempt_id) if row.attempt_id else None
                # Before the close (and so before the verification it triggers): nothing
                # else will ever write this session's pending drops once it is closed.
                self.absorb_pending_drops(session, row, taken)
                self._close_observed(
                    session,
                    row,
                    run=run,
                    attempt=attempt,
                    status=RunStatus.TIMED_OUT,
                    close_reason="idle_window",
                    now=now,
                    message=message,
                )
                session.flush()
                return (run.id, RunStatus.TIMED_OUT.value)
        except ConcurrentUpdateError:
            self.restore_drops(taken)
            return None
        except BaseException:
            self.restore_drops(taken)
            raise
