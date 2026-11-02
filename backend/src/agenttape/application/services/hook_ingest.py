"""The one hook ingest thread per :class:`AppContext` (``POST /hooks/claude-code`` and the
interactive MCP findings path both write through it).

The hooks endpoint never touches the database: it answers ``{}`` and hands the hook to
:meth:`HookIngest.submit`, and ``ObservedSessionService.ingest_hook`` runs here afterwards,
so a locked or slow database can never make a Claude Code tool call wait.

The same thread also writes every interactive MCP finding (``api/routers/mcp.py`` calls
:meth:`HookIngest.in_order`), so an observed run's events get their sequence in the order
AgentTape received the hooks and the finding. Claude Code waits for a hook's answer before
it goes on, so a read the agent did before calling ``record_finding`` was already queued
when the finding arrived, and is always sequenced before it: verification's
``before_sequence`` rule then never skips it for a hook that was simply still queued.

The contract, which ``ObservedSessionService.resolve_for_mcp`` asserts
(:func:`~agenttape.application.services.ingest_state.on_ingest_thread`): hooks and
interactive findings are written only on this thread, one at a time, so the observer
bookkeeping they share (``_note_observer``, the pending dropped counts) needs no
cross-thread ordering of its own.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, TypeVar

from agenttape.application.services.ingest_state import (
    PendingDrops,
    mark_ingest_thread,
    pending_drops,
)
from agenttape.application.services.observed_sessions import ObservedSessionService
from agenttape.capture.claude_hooks import HookEvent, session_hash
from agenttape.worker.hooks import fire_run_terminal

if TYPE_CHECKING:
    from agenttape.application.context import AppContext

log = logging.getLogger(__name__)

T = TypeVar("T")

#: Bound on in-flight+queued ingest jobs (hooks and interactive findings; one ingest
#: thread drains them in order); past this the hooks endpoint answers 503 rather than let
#: memory grow unbounded. Findings are never refused for it (they have their own
#: rate limit), only counted.
MAX_PENDING_HOOKS = 512


class HookIngest:
    """One ingest thread per :class:`AppContext`: keeps ``Pre``/``PostToolUse`` in arrival
    order (a single worker never reorders its own queue), answers ``{}`` immediately, and
    bounds pending work with :data:`MAX_PENDING_HOOKS` so a saturated AgentTape refuses new
    hooks (503) instead of queuing forever."""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="agenttape-hooks", initializer=mark_ingest_thread
        )
        #: Run-terminal hooks (context ingest, automatic verification of every citation,
        #: reproduction queueing) for a run a ``SessionEnd`` closed. Their own thread, one
        #: at a time, so a session ending never holds up the next hook or finding from any
        #: session on the ingest thread; still the one ``fire_run_terminal`` path.
        self._terminal = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="agenttape-hooks-terminal"
        )
        self._lock = threading.Lock()
        self._pending = 0
        self._terminal_pending = 0

    @property
    def drops(self) -> PendingDrops:
        return pending_drops(self.ctx)

    def submit(self, hook: HookEvent) -> bool:
        """Queue ``hook`` for ingest. ``False`` when the queue is already full (the
        caller answers 503 and counts the hook as dropped)."""
        with self._lock:
            if self._pending >= MAX_PENDING_HOOKS:
                return False
            self._pending += 1
        dropped = self.take_dropped(session_hash(hook.session_id))
        try:
            self._executor.submit(self._run, hook, dropped)
        except Exception:
            # The job never reached `_run`, so its `finally` never runs either: release
            # the slot (and the drops it carried) here or every future hook eventually gets
            # refused for capacity that was never actually in flight.
            with self._lock:
                self._pending -= 1
            self.mark_dropped(session_hash(hook.session_id), count=dropped)
            raise
        return True

    def in_order(self, fn: Callable[[], T]) -> Future[T]:
        """Run ``fn`` on the ingest thread after every hook already queued, so whatever it
        writes is sequenced after them (an interactive MCP finding, see the module
        docstring). Counted as pending (so :meth:`drain` waits for it) but never refused
        for capacity: the caller has its own rate limit, and a refusal here would drop a
        finding rather than a hook. The slot is released when the job ends, or when it is
        cancelled before it started."""
        with self._lock:
            self._pending += 1
        try:
            future = self._executor.submit(self._job, fn)
        except Exception:
            with self._lock:
                self._pending -= 1
            raise
        future.add_done_callback(self._release)
        return future

    def _release(self, _future: Future[Any]) -> None:
        with self._lock:
            self._pending -= 1

    def _job(self, fn: Callable[[], T]) -> T:
        self._record_unattributed()
        return fn()

    def _record_unattributed(self) -> None:
        """Before any other job: write drops no session could be named for onto every
        session open when they happened, so no hook or finding ingested after them can
        look covered (``ObservedSessionService.record_unattributed_drops``). A failure is
        logged and the count kept for the next job."""
        if self.drops.unattributed() <= 0:
            return
        try:
            ObservedSessionService(self.ctx).record_unattributed_drops()
        except Exception:
            log.exception("could not record unattributed dropped hooks (kept for the next job)")

    def _run(self, hook: HookEvent, dropped: int) -> None:
        try:
            self._record_unattributed()
            try:
                outcome = ObservedSessionService(self.ctx).ingest_hook(hook, dropped=dropped)
            except Exception:
                log.exception("hooks ingest failed for a session (never surfaced to the caller)")
                # Rolled back whole: neither this hook nor the drops it carried reached the
                # row. Count both as dropped, exactly like a refused hook, so the session's
                # window is marked incomplete instead of looking whole without them.
                self.mark_dropped(session_hash(hook.session_id), count=dropped + 1)
                return
            if outcome.reason == "session_rate_limited":
                # The creation limiter refused this session before its run existed, so
                # nothing recorded the hooks dropped so far or this one: carry both to the
                # session's next ingested hook, which then marks its window incomplete.
                self.mark_dropped(session_hash(hook.session_id), count=dropped + 1)
            if outcome.closed_run is not None:
                self._fire_terminal(*outcome.closed_run)
        except Exception:
            log.exception("hooks ingest failed for a session (never surfaced to the caller)")
        finally:
            with self._lock:
                self._pending -= 1

    def _fire_terminal(self, run_id: str, status: str) -> None:
        """Queue the run-terminal hooks for a run this thread's ``SessionEnd`` just closed
        (committed), on the terminal thread."""
        with self._lock:
            self._terminal_pending += 1
        try:
            self._terminal.submit(self._terminal_job, run_id, status)
        except Exception:
            with self._lock:
                self._terminal_pending -= 1
            raise

    def _terminal_job(self, run_id: str, status: str) -> None:
        try:
            asyncio.run(fire_run_terminal(self.ctx, run_id, status))
        except Exception:
            log.exception("run-terminal hooks failed for observed run %s", run_id)
        finally:
            with self._lock:
                self._terminal_pending -= 1

    def mark_dropped(self, hashed_session_id: str, *, count: int = 1) -> None:
        """Count ``count`` hooks as dropped for ``hashed_session_id`` (429/503 before
        ingest, a failed ingest, or a new session refused by the creation limiter);
        surfaced as ``details["dropped_hook_events"]`` plus a WARNING event the next time
        this session's hook is actually ingested."""
        self.drops.mark(hashed_session_id, count)

    def take_dropped(self, hashed_session_id: str) -> int:
        return self.drops.take(hashed_session_id)

    def drain(self, timeout: float = 10.0) -> bool:
        """Block until nothing is pending, run-terminal hooks included (or ``timeout``).
        This is a test seam only
        (``capture_helpers.post_hook`` calls it so existing synchronous assertions keep
        working); production code never calls it: the hooks endpoint always answers
        before ingest runs, by design. Safe to call from anywhere."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._pending == 0 and self._terminal_pending == 0:
                    return True
            time.sleep(0.005)
        return False


_creation_lock = threading.Lock()


def hook_ingest(ctx: AppContext) -> HookIngest:
    """The one :class:`HookIngest` for ``ctx`` (stored on ``ctx.extensions``)."""
    with _creation_lock:
        ingest = ctx.extensions.get("hook_ingest")
        if not isinstance(ingest, HookIngest):
            ingest = HookIngest(ctx)
            ctx.extensions["hook_ingest"] = ingest
        return ingest
