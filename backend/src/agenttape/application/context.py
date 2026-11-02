"""Application context: the one object every entry point (API, CLI, worker, scheduler)
builds to reach the shared services. Construct with :func:`agenttape.bootstrap.build_context`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from agenttape.adapters.registry import AdapterRegistry
from agenttape.application.notifier import EventNotifier
from agenttape.artifacts.store import ArtifactStore
from agenttape.config import Settings
from agenttape.evaluations.base import EvaluatorRegistry
from agenttape.infrastructure.clock import Clock
from agenttape.infrastructure.db.engine import session_scope
from agenttape.infrastructure.leases import DbLeaseManager
from agenttape.infrastructure.queue import DbQueue
from agenttape.security.attempt_redactors import AttemptRedactors


@dataclass
class AppContext:
    settings: Settings
    engine: Engine
    session_factory: sessionmaker[Session]
    clock: Clock
    queue: DbQueue
    leases: DbLeaseManager
    artifact_store: ArtifactStore
    adapters: AdapterRegistry
    evaluators: EvaluatorRegistry
    notifier: EventNotifier
    #: Free-form slot for extensions (e.g. the scheduler registers itself here).
    extensions: dict[str, Any] = field(default_factory=dict)
    #: Each attempt running in this process's worker, with its secret-value redactor, for
    #: events recorded for it outside the worker (hooks, MCP findings). In memory only.
    attempt_redactors: AttemptRedactors = field(default_factory=AttemptRedactors)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Transactional session: commits on success, rolls back on error."""
        with session_scope(self.session_factory) as session:
            yield session

    def close(self) -> None:
        self.engine.dispose()
