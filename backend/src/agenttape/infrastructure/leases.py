"""Named single-owner leases (``scheduler_leases`` table).

Used by the scheduler so exactly one process evaluates due schedules, and by
any other loop that must have a single active owner. Acquisition is a conditional
insert/update, portable across SQLite and PostgreSQL.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from agenttape.infrastructure.clock import Clock
from agenttape.infrastructure.db.models import SchedulerLease


class DbLeaseManager:
    def __init__(self, session_factory: sessionmaker[Session], clock: Clock) -> None:
        self._sf = session_factory
        self._clock = clock

    def acquire(self, name: str, owner_id: str, ttl_seconds: float) -> bool:
        """Acquire or renew ``name`` for ``owner_id``. Returns True when held after the call."""
        now = self._clock.now()
        expires = now + timedelta(seconds=ttl_seconds)
        with self._sf() as session:
            result = session.execute(
                update(SchedulerLease)
                .where(
                    SchedulerLease.name == name,
                    or_(SchedulerLease.owner_id == owner_id, SchedulerLease.expires_at < now),
                )
                .values(owner_id=owner_id, renewed_at=now, expires_at=expires)
            )
            if (result.rowcount or 0) == 1:  # type: ignore[attr-defined]
                session.commit()
                return True
            session.rollback()
        with self._sf() as session:
            try:
                session.add(
                    SchedulerLease(
                        name=name,
                        owner_id=owner_id,
                        acquired_at=now,
                        renewed_at=now,
                        expires_at=expires,
                    )
                )
                session.commit()
                return True
            except IntegrityError:
                session.rollback()
                return False

    def release(self, name: str, owner_id: str) -> None:
        with self._sf() as session:
            session.execute(
                delete(SchedulerLease).where(
                    SchedulerLease.name == name, SchedulerLease.owner_id == owner_id
                )
            )
            session.commit()

    def holder(self, name: str) -> tuple[str, datetime] | None:
        with self._sf() as session:
            lease = session.get(SchedulerLease, name)
            if lease is None or lease.expires_at < self._clock.now():
                return None
            return lease.owner_id, lease.expires_at
