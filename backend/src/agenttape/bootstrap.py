"""Composition root: build an :class:`AppContext` from settings.

Every entry point (``agenttape serve``, ``agenttape worker``, CLI commands, tests) calls
:func:`build_context` so they all share the same wiring.
"""

from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.orm import Session

from agenttape.adapters.registry import AdapterRegistry
from agenttape.application.context import AppContext
from agenttape.application.notifier import EventNotifier
from agenttape.config import Settings, load_settings
from agenttape.evaluations.base import EvaluatorRegistry
from agenttape.infrastructure.artifact_store import FilesystemArtifactStore
from agenttape.infrastructure.clock import Clock, SystemClock
from agenttape.infrastructure.db.engine import make_engine, make_session_factory
from agenttape.infrastructure.db.migrate import upgrade
from agenttape.infrastructure.leases import DbLeaseManager
from agenttape.infrastructure.queue import DbQueue
from agenttape.security.persistence import install_redaction_guard


def build_context(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    migrate: bool = False,
) -> AppContext:
    settings = settings or load_settings()
    settings.ensure_dirs()
    url = settings.resolved_database_url
    if migrate:
        upgrade(url)
    engine = make_engine(url)
    session_factory = make_session_factory(engine)
    clock = clock or SystemClock()
    notifier = EventNotifier()

    def _after_commit(session: Session) -> None:
        runs: set[str] = session.info.pop("agenttape_notify_runs", set())
        for run_id in runs:
            notifier.notify(run_id)

    def _after_rollback(session: Session) -> None:
        session.info.pop("agenttape_notify_runs", None)

    event.listen(session_factory, "after_commit", _after_commit)
    install_redaction_guard(session_factory)
    event.listen(session_factory, "after_soft_rollback", lambda s, _t: _after_rollback(s))

    ctx = AppContext(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        clock=clock,
        queue=DbQueue(session_factory, clock),
        leases=DbLeaseManager(session_factory, clock),
        artifact_store=FilesystemArtifactStore(settings.artifacts_dir, settings.max_artifact_bytes),
        adapters=AdapterRegistry.load(),
        evaluators=EvaluatorRegistry.load(),
        notifier=notifier,
    )
    return ctx
