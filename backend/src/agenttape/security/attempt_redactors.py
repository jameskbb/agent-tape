"""Each running attempt's redactor, for events recorded outside the worker's own sink.

The worker redacts an attempt's events with a redactor that holds the secret values the
attempt resolved (``worker/executors/agent.py``). Events for the same attempt can also
arrive through Claude Code hooks (a Herdr-hosted session) or the MCP capture server, which
are recorded on the API side. :class:`AttemptRedactors` lets those paths redact with the
same values: the worker registers the attempt's redactor for as long as the attempt runs,
and the hook ingest and capture service look it up by attempt id.

The registry lives in memory in one process and is never persisted or logged. A worker in
another process (``agenttape worker``, or ``agenttape serve --no-worker``) registers
nothing here, so its attempts' hook events fall back to :func:`configured_redactor`: the
values of ``AGENTTAPE_REDACT_ENV`` plus the credential patterns.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager

from agenttape.security.redaction import Redactor


def configured_redactor(redact_env: Iterable[str]) -> Redactor:
    """A redactor seeded with the values of the ``AGENTTAPE_REDACT_ENV`` variables."""
    values = [os.environ.get(name, "") for name in redact_env]
    return Redactor([v for v in values if v])


class AttemptRedactors:
    """Attempt id -> that attempt's redactor, for attempts running in this process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_attempt: dict[str, Redactor] = {}

    @contextmanager
    def registered(self, attempt_id: str, redactor: Redactor) -> Iterator[None]:
        """Publish ``redactor`` for ``attempt_id`` while the block runs."""
        with self._lock:
            self._by_attempt[attempt_id] = redactor
        try:
            yield
        finally:
            with self._lock:
                if self._by_attempt.get(attempt_id) is redactor:
                    del self._by_attempt[attempt_id]

    def get(self, attempt_id: str | None) -> Redactor | None:
        if not attempt_id:
            return None
        with self._lock:
            return self._by_attempt.get(attempt_id)

    def for_attempt(self, attempt_id: str | None, redact_env: Iterable[str]) -> Redactor:
        """The attempt's registered redactor, or :func:`configured_redactor` when none is."""
        return self.get(attempt_id) or configured_redactor(redact_env)
