"""Database-backed queue with leases (ADR 0003).

Claiming is a two-step, portable compare-and-set: read a small batch of candidate ids in
priority/FIFO order, then for each candidate issue

    UPDATE queue_items SET status='leased', lease_owner=:me, ...
    WHERE id=:id AND status='queued'

in its own transaction and accept the first row whose ``rowcount == 1``. The conditional
UPDATE is atomic on SQLite (single writer) and PostgreSQL (row lock + WHERE re-check under
READ COMMITTED), so two workers can never both lease the same item. No SQLite-specific or
PostgreSQL-specific SQL is used.

Leased items whose lease expired are *not* silently re-claimed here: the worker's
reconciliation pass first settles the owning attempt (marking it ``interrupted``) and then
re-enqueues the run if a retry is allowed. That keeps "one active executor per run" true.

Claim guards (additive): :func:`register_claim_guard` adds a check that runs
*after* a claim commits. A guard returns a reason string to defer the item (it goes back
to ``queued`` with a short delay) or ``None`` to admit it. Checking after the commit means
concurrent claimers always see each other's leases, so a guard that counts leased items
(concurrency limits) can never admit more than its limit.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from agenttape.application.ports import ClaimedItem
from agenttape.domain.enums import QueueItemStatus
from agenttape.infrastructure.clock import Clock
from agenttape.infrastructure.db.models import QueueItem

CANDIDATE_BATCH = 8
#: Seconds a deferred item waits before it is claimable again (plus up to 50% jitter).
DEFER_SECONDS = 1.0

log = logging.getLogger("agenttape.queue")

#: ``(session, leased item) -> reason to defer, or None to admit``.
ClaimGuard = Callable[[Session, QueueItem], str | None]
_CLAIM_GUARDS: list[ClaimGuard] = []


def register_claim_guard(fn: ClaimGuard) -> None:
    """Add a post-claim admission check (e.g. concurrency limits). Idempotent."""
    if fn not in _CLAIM_GUARDS:
        _CLAIM_GUARDS.append(fn)


def claim_guards() -> list[ClaimGuard]:
    return list(_CLAIM_GUARDS)


class DbQueue:
    def __init__(self, session_factory: sessionmaker[Session], clock: Clock) -> None:
        self._sf = session_factory
        self._clock = clock

    # ------------------------------------------------------------------ producer side

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
    ) -> str:
        now = self._clock.now()
        item = QueueItem(
            dedupe_key=dedupe_key,
            kind=kind,
            run_id=run_id,
            priority=priority,
            status=QueueItemStatus.QUEUED,
            payload=payload or {},
            enqueued_at=now,
            available_at=available_at or now,
        )
        session.add(item)
        session.flush()
        return item.id

    def cancel_for_run(self, session: Session, run_id: str) -> int:
        result = session.execute(
            update(QueueItem)
            .where(
                QueueItem.run_id == run_id,
                QueueItem.status == QueueItemStatus.QUEUED,
            )
            .values(status=QueueItemStatus.CANCELLED, completed_at=self._clock.now())
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ consumer side

    def claim(
        self, worker_id: str, lease_seconds: float, kinds: tuple[str, ...] = ("run",)
    ) -> ClaimedItem | None:
        for _round in range(3):
            now = self._clock.now()
            with self._sf() as session:
                candidates = list(
                    session.scalars(
                        select(QueueItem.id)
                        .where(
                            QueueItem.status == QueueItemStatus.QUEUED,
                            QueueItem.kind.in_(kinds),
                            QueueItem.available_at <= now,
                        )
                        .order_by(
                            QueueItem.priority.desc(),
                            QueueItem.enqueued_at.asc(),
                            QueueItem.id.asc(),
                        )
                        .limit(CANDIDATE_BATCH)
                    )
                )
            if not candidates:
                return None
            for item_id in candidates:
                claimed = self._try_claim(item_id, worker_id, lease_seconds)
                if claimed is not None and self._admit(claimed, worker_id):
                    return claimed
        return None

    def _admit(self, claimed: ClaimedItem, worker_id: str) -> bool:
        """Run claim guards on a freshly leased item; defer it if any guard objects."""
        guards = claim_guards()
        if not guards:
            return True
        with self._sf() as session:
            item = session.get(QueueItem, claimed.id)
            if item is None:
                return False
            reason: str | None = None
            for guard in guards:
                try:
                    reason = guard(session, item)
                except Exception:
                    log.exception("claim guard %r failed; admitting item %s", guard, item.id)
                    reason = None
                if reason:
                    break
            if not reason:
                session.commit()
                return True
            if item.status == QueueItemStatus.LEASED and item.lease_owner == worker_id:
                delay = DEFER_SECONDS * (1 + random.random() / 2)
                item.status = QueueItemStatus.QUEUED
                item.lease_owner = None
                item.lease_expires_at = None
                item.claim_count = max(0, item.claim_count - 1)
                item.available_at = self._clock.now() + timedelta(seconds=delay)
            session.commit()
            return False

    def _try_claim(self, item_id: str, worker_id: str, lease_seconds: float) -> ClaimedItem | None:
        now = self._clock.now()
        expires = now + timedelta(seconds=lease_seconds)
        with self._sf() as session:
            result = session.execute(
                update(QueueItem)
                .where(QueueItem.id == item_id, QueueItem.status == QueueItemStatus.QUEUED)
                .values(
                    status=QueueItemStatus.LEASED,
                    lease_owner=worker_id,
                    lease_expires_at=expires,
                    last_claimed_at=now,
                    claim_count=QueueItem.claim_count + 1,
                )
            )
            if (result.rowcount or 0) != 1:  # type: ignore[attr-defined]
                session.rollback()
                return None
            session.commit()
            item = session.get(QueueItem, item_id)
            assert item is not None
            return ClaimedItem(
                id=item.id,
                kind=item.kind,
                run_id=item.run_id,
                payload=dict(item.payload or {}),
                priority=item.priority,
                claim_count=item.claim_count,
                lease_expires_at=expires,
            )

    def renew(self, item_id: str, worker_id: str, lease_seconds: float) -> bool:
        with self._sf() as session:
            result = session.execute(
                update(QueueItem)
                .where(
                    QueueItem.id == item_id,
                    QueueItem.status == QueueItemStatus.LEASED,
                    QueueItem.lease_owner == worker_id,
                )
                .values(lease_expires_at=self._clock.now() + timedelta(seconds=lease_seconds))
            )
            session.commit()
            return (result.rowcount or 0) == 1  # type: ignore[attr-defined]

    def complete(self, item_id: str, worker_id: str) -> bool:
        with self._sf() as session:
            result = session.execute(
                update(QueueItem)
                .where(
                    QueueItem.id == item_id,
                    QueueItem.status == QueueItemStatus.LEASED,
                    QueueItem.lease_owner == worker_id,
                )
                .values(status=QueueItemStatus.DONE, completed_at=self._clock.now())
            )
            session.commit()
            return (result.rowcount or 0) == 1  # type: ignore[attr-defined]

    def expired_leases(self, session: Session) -> list[QueueItem]:
        return list(
            session.scalars(
                select(QueueItem).where(
                    QueueItem.status == QueueItemStatus.LEASED,
                    QueueItem.lease_expires_at < self._clock.now(),
                )
            )
        )

    def position(self, session: Session, run_id: str) -> int | None:
        """1-based position among ready queued items, or None if the run is not queued."""
        item = session.scalars(
            select(QueueItem)
            .where(QueueItem.run_id == run_id, QueueItem.status == QueueItemStatus.QUEUED)
            .order_by(QueueItem.enqueued_at.desc())
            .limit(1)
        ).first()
        if item is None:
            return None
        ahead = session.scalar(
            select(func.count())
            .select_from(QueueItem)
            .where(
                QueueItem.status == QueueItemStatus.QUEUED,
                QueueItem.kind == item.kind,
                QueueItem.id != item.id,
                or_(
                    QueueItem.priority > item.priority,
                    and_(
                        QueueItem.priority == item.priority,
                        or_(
                            QueueItem.enqueued_at < item.enqueued_at,
                            and_(
                                QueueItem.enqueued_at == item.enqueued_at,
                                QueueItem.id < item.id,
                            ),
                        ),
                    ),
                ),
            )
        )
        return int(ahead or 0) + 1

    def depth(self, session: Session) -> dict[str, int]:
        rows = session.execute(
            select(QueueItem.status, func.count()).group_by(QueueItem.status)
        ).all()
        return {str(status): int(count) for status, count in rows}
