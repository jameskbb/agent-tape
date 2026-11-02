"""Registers the queue worker as a background service (started by ``agenttape serve``)."""

from __future__ import annotations

from agenttape.application.context import AppContext
from agenttape.runtime.base import BackgroundService, ServiceSpec, register_service
from agenttape.worker.worker import Worker


def _factory(ctx: AppContext) -> BackgroundService:
    return Worker(ctx, kind="worker")


register_service(
    ServiceSpec(
        name="worker",
        factory=_factory,
        description="Claims queued runs and executes them through the adapter registry",
    )
)
