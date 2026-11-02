"""Batching, size-capped, redacting event sink used by the worker for one attempt.

* stdout/stderr lines are coalesced into chunk events, one pending batch per stream
  (flushed every ``FLUSH_INTERVAL`` seconds, or when a batch reaches ``BATCH_BYTES`` or
  ``BATCH_LINES``), so interleaved output still batches well;
* once an attempt has stored ``STREAM_EVENT_BUDGET`` bytes of a stream as events, further
  chunks are dropped from the database (one warning event says so) - the full redacted
  output is still in the raw log file;
* every other event is written in order after pending chunks are flushed;
* database writes run in a worker thread so the event loop keeps draining pipes.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field

from sqlalchemy.orm import Session, sessionmaker

from agenttape.application.events import EventRecorder
from agenttape.domain.enums import EventType, Severity
from agenttape.domain.events import EventDraft
from agenttape.security.redaction import Redactor

FLUSH_INTERVAL = 0.25
BATCH_BYTES = 8 * 1024
BATCH_LINES = 200
STREAM_EVENT_BUDGET = 2 * 1024 * 1024

_CHUNK_TYPES = (EventType.STDOUT, EventType.STDERR)
BatchKey = tuple[EventType, str | None, str | None]


@dataclass
class _Batch:
    type: EventType
    attempt_id: str | None
    step_run_id: str | None
    node_key: str | None
    source: str
    lines: list[str] = field(default_factory=list)
    size: int = 0
    first_mono: float | None = None
    truncated: bool = False


def _batch_draft(b: _Batch) -> EventDraft:
    first = b.lines[0].strip() or "(blank line)"
    more = len(b.lines) - 1
    summary = first[:180] + (f"  (+{more} more line{'s' if more != 1 else ''})" if more else "")
    payload: dict[str, object] = {
        "text": "\n".join(b.lines),
        "lines": len(b.lines),
        "stream": b.type.value,
    }
    if b.truncated:
        payload["truncated"] = True
    return EventDraft(
        type=b.type,
        summary=summary,
        payload=payload,
        severity=Severity.INFO if b.type == EventType.STDOUT else Severity.NOTICE,
        attempt_id=b.attempt_id,
        step_run_id=b.step_run_id,
        node_key=b.node_key,
        source=b.source,
        monotonic_ms=b.first_mono,
    )


class BatchingEventSink:
    def __init__(
        self,
        *,
        run_id: str,
        recorder: EventRecorder,
        session_factory: sessionmaker[Session],
        redactor: Redactor,
        started_monotonic: float,
        stream_budget: int = STREAM_EVENT_BUDGET,
    ) -> None:
        self.run_id = run_id
        self._recorder = recorder
        self._sf = session_factory
        self._redactor = redactor
        self._t0 = started_monotonic
        self._budget = stream_budget
        self._stored: dict[EventType, int] = {EventType.STDOUT: 0, EventType.STDERR: 0}
        self._over_budget_notified: set[EventType] = set()
        self._batches: dict[BatchKey, _Batch] = {}
        self._lock = asyncio.Lock()
        self._flusher: asyncio.Task[None] | None = None
        self._closed = False
        self.events_written = 0

    def _mono(self) -> float:
        return (time.monotonic() - self._t0) * 1000.0

    async def start(self) -> None:
        if self._flusher is None:
            self._flusher = asyncio.create_task(self._flush_loop())

    async def _flush_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(FLUSH_INTERVAL)
            await self.flush()

    async def emit(self, draft: EventDraft) -> None:
        if draft.monotonic_ms is None:
            draft.monotonic_ms = self._mono()
        etype = EventType(draft.type)
        if etype in _CHUNK_TYPES:
            await self._add_chunk(etype, draft)
            return
        async with self._lock:
            await self._write([*self._take_batches(), draft])

    async def _add_chunk(self, etype: EventType, draft: EventDraft) -> None:
        text = str(draft.payload.get("text", ""))
        size = len(text.encode("utf-8")) + 1
        key: BatchKey = (etype, draft.attempt_id, draft.node_key)
        async with self._lock:
            if self._stored[etype] >= self._budget:
                if etype not in self._over_budget_notified:
                    self._over_budget_notified.add(etype)
                    notice = EventDraft(
                        type=EventType.WARNING,
                        summary=f"{etype.value} exceeded the stored-event budget; "
                        "the rest is only in the raw log",
                        payload={"stream": etype.value, "budget_bytes": self._budget},
                        severity=Severity.WARNING,
                        attempt_id=draft.attempt_id,
                        source=draft.source,
                        monotonic_ms=draft.monotonic_ms,
                    )
                    await self._write([*self._take_batches(), notice])
                return
            batch = self._batches.get(key)
            if batch is None:
                batch = self._batches[key] = _Batch(
                    type=etype,
                    attempt_id=draft.attempt_id,
                    step_run_id=draft.step_run_id,
                    node_key=draft.node_key,
                    source=draft.source,
                    first_mono=draft.monotonic_ms,
                )
            batch.lines.append(text)
            batch.size += size
            batch.truncated = batch.truncated or bool(draft.payload.get("truncated"))
            self._stored[etype] += size
            if batch.size >= BATCH_BYTES or len(batch.lines) >= BATCH_LINES:
                del self._batches[key]
                await self._write([_batch_draft(batch)])

    def _take_batches(self) -> list[EventDraft]:
        """Pending chunk batches (at most one per stream), oldest first."""
        batches = sorted(self._batches.values(), key=lambda b: b.first_mono or 0.0)
        self._batches.clear()
        return [_batch_draft(b) for b in batches if b.lines]

    async def _write(self, drafts: list[EventDraft]) -> None:
        if not drafts:
            return

        def _do() -> None:
            with self._sf() as session:
                self._recorder.append(session, self.run_id, drafts, redactor=self._redactor)
                session.commit()

        await asyncio.to_thread(_do)
        self.events_written += len(drafts)

    async def flush(self) -> None:
        async with self._lock:
            await self._write(self._take_batches())

    async def close(self) -> None:
        self._closed = True
        if self._flusher is not None:
            self._flusher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flusher
        await self.flush()
