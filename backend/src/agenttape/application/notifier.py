"""In-process wake-ups for live event streams.

Writers call :meth:`EventNotifier.notify` after committing events; SSE handlers
``await wait(run_id, timeout)``. Cross-process writers (a separate ``agenttape worker``)
are covered by the SSE handler's periodic poll, so this is an optimisation, never a
correctness requirement.
"""

from __future__ import annotations

import asyncio
import threading
from collections import defaultdict


class EventNotifier:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._waiters: dict[str, set[tuple[asyncio.AbstractEventLoop, asyncio.Event]]] = (
            defaultdict(set)
        )

    def notify(self, run_id: str) -> None:
        with self._lock:
            waiters = list(self._waiters.get(run_id, ()))
        for loop, event in waiters:
            if loop.is_closed():
                continue
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:  # loop closed between the check and the call
                continue

    async def wait(self, run_id: str, timeout: float) -> bool:
        """Wait until notified for ``run_id`` or ``timeout`` elapses. True if notified."""
        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        key = (loop, event)
        with self._lock:
            self._waiters[run_id].add(key)
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False
        finally:
            with self._lock:
                waiters = self._waiters.get(run_id)
                if waiters is not None:
                    waiters.discard(key)
                    if not waiters:
                        self._waiters.pop(run_id, None)
