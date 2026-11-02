"""State the Claude Code hook ingest thread shares with the services it runs.

Its own module, importing nothing from the services, so both
:mod:`agenttape.application.services.hook_ingest` (which runs every hook and interactive
MCP finding on one thread) and :mod:`agenttape.application.services.observed_sessions`
(which writes what that thread decides onto ``observed_sessions`` rows) can use it without
importing each other.

* :func:`on_ingest_thread` says whether the caller is running on a hook ingest thread.
  The ingest contract is that every hook and every interactive MCP finding is written
  there, one at a time, in the order AgentTape received them; code that depends on that
  order (``ObservedSessionService.resolve_for_mcp``) asserts it.
* :class:`PendingDrops` holds hooks AgentTape received but could not record (refused with
  429/503, or answered and then lost to a failed ingest), per session, until something
  writes them onto that session's row (``details["dropped_hook_events"]``): the session's
  next ingested hook, the next interactive finding resolved to that session (so a drop
  that precedes a finding is always recorded before it), or the idle sweep that closes
  the row. Only a restart loses them, and the restart is then a gap of its own.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenttape.application.context import AppContext

#: Bound on sessions with a dropped-hook count waiting to be recorded. A session refused
#: by the creation limiter may never get a run (a flood of fresh session ids), so past
#: this the oldest pending count stops being attributed to its session and becomes an
#: unattributed drop instead (:meth:`PendingDrops.mark`), never simply forgotten.
MAX_TRACKED_DROPPED_SESSIONS = 10_000

_marker = threading.local()


def mark_ingest_thread() -> None:
    """``ThreadPoolExecutor`` initializer for the hook ingest thread."""
    _marker.ingest = True


def on_ingest_thread() -> bool:
    """Whether the caller runs on a hook ingest thread (see the module docstring)."""
    return bool(getattr(_marker, "ingest", False))


class PendingDrops:
    """Dropped-hook counts not yet written to a row, one :class:`AppContext` each.

    Per session (keyed by ``session_hash``) while the session is known; *unattributed*
    when it is not (a hook abandoned before its body was read, a body that could not be
    parsed, or a per-session count evicted by :data:`MAX_TRACKED_DROPPED_SESSIONS`). An
    unattributed drop could belong to any session, so it is written onto every observed or
    Herdr-hosted row open when it happened (``ObservedSessionService.
    record_unattributed_drops``). Thread-safe: marked from the event loop, taken on the
    ingest thread and by the idle sweep.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_session: dict[str, int] = {}
        self._unattributed = 0
        self._unattributed_since: datetime | None = None

    def mark(self, hashed_session_id: str, count: int = 1) -> None:
        if count <= 0:
            return
        with self._lock:
            if (
                hashed_session_id not in self._by_session
                and len(self._by_session) >= MAX_TRACKED_DROPPED_SESSIONS
            ):
                evicted = self._by_session.pop(next(iter(self._by_session)))
                self._add_unattributed(evicted, None)
            self._by_session[hashed_session_id] = self._by_session.get(hashed_session_id, 0) + count

    def take(self, hashed_session_id: str) -> int:
        with self._lock:
            return self._by_session.pop(hashed_session_id, 0)

    def mark_unattributed(self, count: int = 1, *, at: datetime | None = None) -> None:
        """Count ``count`` hooks lost with no known session, at ``at`` (the earliest such
        time is kept, so rows closed since then are still covered)."""
        if count <= 0:
            return
        with self._lock:
            self._add_unattributed(count, at)

    def _add_unattributed(self, count: int, at: datetime | None) -> None:
        self._unattributed += count
        if at is not None and (self._unattributed_since is None or at < self._unattributed_since):
            self._unattributed_since = at

    def unattributed(self) -> int:
        with self._lock:
            return self._unattributed

    def take_unattributed(self) -> tuple[int, datetime | None]:
        """``(count, since)``: every unattributed drop so far and the earliest time one
        was marked (``None`` when none carried a time), resetting both."""
        with self._lock:
            taken = (self._unattributed, self._unattributed_since)
            self._unattributed = 0
            self._unattributed_since = None
            return taken


_creation_lock = threading.Lock()


def pending_drops(ctx: AppContext) -> PendingDrops:
    """The one :class:`PendingDrops` for ``ctx`` (stored on ``ctx.extensions``)."""
    with _creation_lock:
        drops = ctx.extensions.get("pending_hook_drops")
        if not isinstance(drops, PendingDrops):
            drops = PendingDrops()
            ctx.extensions["pending_hook_drops"] = drops
        return drops
