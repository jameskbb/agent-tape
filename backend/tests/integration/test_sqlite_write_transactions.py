"""Write transactions on SQLite: why the normalizer hit "database is locked" and the fix.

In WAL mode a deferred transaction that reads and then writes must upgrade its read
snapshot to a write lock. If another connection committed in between, SQLite refuses the
upgrade at once (``SQLITE_BUSY_SNAPSHOT``, reported as "database is locked"); the
busy_timeout never applies. ``begin_immediate`` takes the write lock up front, so a
concurrent writer waits on busy_timeout instead.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from agenttape.infrastructure.db.engine import begin_immediate, make_engine, make_session_factory


def _engine(tmp_path: Path) -> Engine:
    engine = make_engine(f"sqlite:///{tmp_path / 'w.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE marks (label TEXT NOT NULL)"))
    return engine


def _insert_in_thread(
    engine: Engine, label: str, started: threading.Event
) -> tuple[threading.Thread, list[BaseException]]:
    errors: list[BaseException] = []

    def write() -> None:
        try:
            with make_session_factory(engine)() as other:
                started.set()
                other.execute(text("INSERT INTO marks (label) VALUES (:l)"), {"l": label})
                other.commit()
        except BaseException as exc:  # surfaced to the test thread after join
            errors.append(exc)

    return threading.Thread(target=write), errors


def _labels(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        return set(conn.execute(text("SELECT label FROM marks")).scalars())


def test_a_savepoint_read_then_write_fails_when_another_connection_commits_first(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    session = make_session_factory(engine)()
    try:
        session.begin_nested()  # SAVEPOINT: opens a deferred transaction on pysqlite
        session.execute(text("SELECT count(*) FROM marks")).scalar()
        other, errors = _insert_in_thread(engine, "other", threading.Event())
        other.start()
        other.join()
        assert errors == []
        with pytest.raises(OperationalError, match="database is locked"):
            session.execute(text("INSERT INTO marks (label) VALUES ('mine')"))
    finally:
        session.rollback()
        session.close()
    assert _labels(engine) == {"other"}
    engine.dispose()


def test_begin_immediate_makes_the_concurrent_writer_wait(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    session = make_session_factory(engine)()
    started = threading.Event()
    other, errors = _insert_in_thread(engine, "other", started)
    try:
        begin_immediate(session)
        with session.begin_nested():
            session.execute(text("SELECT count(*) FROM marks")).scalar()
            other.start()
            assert started.wait(10)
            # The writer cannot finish before this session commits: it is queued on
            # busy_timeout behind our write lock (without the lock it commits at once and
            # the INSERT below fails with "database is locked").
            other.join(timeout=1.0)
            assert other.is_alive()
            session.execute(text("INSERT INTO marks (label) VALUES ('mine')"))
        session.commit()
    finally:
        session.close()
    other.join()
    assert errors == []
    assert _labels(engine) == {"mine", "other"}
    engine.dispose()


def test_begin_immediate_is_a_no_op_on_postgresql() -> None:
    calls: list[Any] = []

    class _Dialect:
        name = "postgresql"

    class _Bind:
        dialect = _Dialect()

    class _Session:
        def get_bind(self) -> _Bind:
            return _Bind()

        def execute(self, statement: Any) -> None:
            calls.append(statement)

    begin_immediate(cast(Session, _Session()))
    assert calls == []
