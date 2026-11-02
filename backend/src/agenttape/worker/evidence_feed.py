"""One read of an attempt's new ``events`` rows, shared by the evidence collectors.

File Evidence (telemetry -> file operations) and Context Evidence (normalization) both
follow the events an attempt persists. Rather than each tailing the table every second
with its own cursor and session, each subscribes to the attempt's :class:`EventFeed` with
the event types it wants and its own starting sequence. A poll reads the new rows once, in
one session, and hands every subscriber its share (in sequence order) inside that session.

Polls are throttled: a poll within ``POLL_SECONDS`` of the previous one is a no-op unless
forced, so several monitors may call :meth:`EventFeed.poll` every second and the database
is still read once. A final flush forces a poll so nothing persisted before the adapter
returned is missed.
"""

from __future__ import annotations

import logging
import threading
import time
import weakref
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from agenttape.infrastructure.db.engine import begin_immediate
from agenttape.infrastructure.db.models import Event

if TYPE_CHECKING:
    from agenttape.application.context import AppContext

log = logging.getLogger(__name__)
POLL_SECONDS = 1.0
BATCH = 500

Consumer = Callable[[Session, list[Event]], None]
"""(session, events): called in the polling thread; must not commit the session."""


@dataclass
class _Subscriber:
    types: frozenset[str]
    consume: Consumer
    cursor: int


class EventFeed:
    """New events of one attempt, read once per poll and handed to every subscriber."""

    def __init__(self, ctx: AppContext, run_id: str, attempt_id: str) -> None:
        self.ctx = ctx
        self.run_id = run_id
        self.attempt_id = attempt_id
        #: Held while polling; take it to read state a consumer fills in.
        self.lock = threading.RLock()
        self._subscribers: list[_Subscriber] = []
        self._last_poll = float("-inf")

    def subscribe(self, types: Iterable[str], consume: Consumer, *, after: int) -> None:
        """Receive events of ``types`` with a sequence above ``after`` on every poll."""
        with self.lock:
            self._subscribers.append(_Subscriber(frozenset(types), consume, after))

    def poll(self, *, force: bool = False) -> None:
        """Read every new event (in batches) and dispatch it. Blocking: call in a thread."""
        with self.lock:
            now = time.monotonic()
            if not self._subscribers or (not force and now - self._last_poll < POLL_SECONDS):
                return
            self._last_poll = now
            while self._read_batch() >= BATCH:
                pass

    def _read_batch(self) -> int:
        after = min(s.cursor for s in self._subscribers)
        types = sorted(set().union(*(s.types for s in self._subscribers)))
        with self.ctx.session() as session:
            events = list(
                session.scalars(
                    select(Event)
                    .where(
                        Event.run_id == self.run_id,
                        Event.attempt_id == self.attempt_id,
                        Event.sequence > after,
                        Event.type.in_(types),
                    )
                    .order_by(Event.sequence)
                    .limit(BATCH)
                )
            )
            if not events:
                return 0
            # The SELECT above ran outside a transaction; take the write lock before the
            # consumers read-then-write, so a concurrent commit makes them wait, not fail.
            begin_immediate(session)
            for sub in self._subscribers:
                mine = [e for e in events if e.sequence > sub.cursor and e.type in sub.types]
                sub.cursor = max(sub.cursor, events[-1].sequence)
                if mine:
                    try:
                        sub.consume(session, mine)
                    except Exception:  # one consumer never starves the others
                        log.exception("evidence feed consumer failed for run %s", self.run_id)
        return len(events)


_FEEDS: weakref.WeakValueDictionary[str, EventFeed] = weakref.WeakValueDictionary()
_FEEDS_LOCK = threading.Lock()


def feed_for(ctx: AppContext, run_id: str, attempt_id: str) -> EventFeed:
    """The attempt's feed; it lives as long as a subscriber holds a reference to it."""
    with _FEEDS_LOCK:
        feed = _FEEDS.get(attempt_id)
        if feed is None:
            feed = EventFeed(ctx, run_id, attempt_id)
            _FEEDS[attempt_id] = feed
        return feed
