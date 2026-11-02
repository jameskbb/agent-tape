"""Engine and session factory. SQLite gets WAL, a busy timeout, and foreign keys."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

SQLITE_BUSY_TIMEOUT_MS = 10_000


def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def make_engine(url: str, *, echo: bool = False) -> Engine:
    kwargs: dict[str, Any] = {"echo": echo, "future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {
            "check_same_thread": False,
            "timeout": SQLITE_BUSY_TIMEOUT_MS / 1000,
        }
    else:
        kwargs["pool_pre_ping"] = True
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        event.listen(engine, "connect", _sqlite_pragmas)
    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def begin_immediate(session: Session) -> None:
    """Take SQLite's write lock now, so this session's writes queue instead of failing.

    pysqlite opens deferred transactions. In WAL mode a deferred transaction that reads
    and then writes has to upgrade its read snapshot to a write lock; if another connection
    committed in between, SQLite refuses the upgrade at once (``SQLITE_BUSY_SNAPSHOT``,
    reported as "database is locked") and the busy_timeout never applies. The timeout only
    helps a transaction that asks for the write lock up front, which ``BEGIN IMMEDIATE``
    does. Call it as the first statement that opens a transaction on the session's
    connection (pysqlite runs a plain SELECT outside any transaction, so a read before it is
    fine; a write or SAVEPOINT before it is not). PostgreSQL has no such upgrade failure,
    so there it does nothing.
    """
    if session.get_bind().dialect.name == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on error."""
    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
