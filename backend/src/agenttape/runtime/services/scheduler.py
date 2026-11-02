"""Registers the scheduler as a background service (started by ``agenttape serve``).

``agenttape scheduler`` runs the same loop standalone. Only the instance holding the
``scheduler`` database lease creates occurrences; any others wait as standbys.
"""

from __future__ import annotations

from agenttape.application.context import AppContext
from agenttape.runtime.base import BackgroundService, ServiceSpec, register_service
from agenttape.scheduler.engine import SchedulerLoop


def _factory(ctx: AppContext) -> BackgroundService:
    return SchedulerLoop(ctx)


register_service(
    ServiceSpec(
        name="scheduler",
        factory=_factory,
        description="Creates runs for due schedules (one active owner via a database lease)",
    )
)
