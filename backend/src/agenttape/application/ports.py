"""Interfaces (ports) the application layer depends on.

Concrete implementations live in ``agenttape.infrastructure``; tests and future backends
(e.g. a remote worker queue) can provide alternatives without touching services.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy.orm import Session


@dataclass(frozen=True)
class ClaimedItem:
    id: str
    kind: str
    run_id: str | None
    payload: dict[str, Any]
    priority: int
    claim_count: int
    lease_expires_at: datetime


class Queue(Protocol):
    """Work queue with leases. ``enqueue``/``cancel_for_run`` join the caller's transaction."""

    def enqueue(
        self,
        session: Session,
        *,
        dedupe_key: str,
        run_id: str | None,
        priority: int = 0,
        available_at: datetime | None = None,
        payload: dict[str, Any] | None = None,
        kind: str = "run",
    ) -> str: ...

    def claim(
        self, worker_id: str, lease_seconds: float, kinds: tuple[str, ...] = ("run",)
    ) -> ClaimedItem | None: ...

    def renew(self, item_id: str, worker_id: str, lease_seconds: float) -> bool: ...

    def complete(self, item_id: str, worker_id: str) -> bool: ...

    def cancel_for_run(self, session: Session, run_id: str) -> int: ...

    def position(self, session: Session, run_id: str) -> int | None: ...
