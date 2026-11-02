"""Background services hosted by ``agenttape serve`` (worker, scheduler, ...).

Each module in ``agenttape/runtime/services/`` registers a factory with
:func:`register_service`. ``serve`` starts every registered service unless disabled with
``--no-<name>`` / ``--without <name>``; ``agenttape worker`` and ``agenttape scheduler``
run a single service standalone. The scheduler is added by dropping in
``runtime/services/scheduler.py`` - no edits to ``serve`` are needed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from agenttape.application.context import AppContext
from agenttape.plugins import import_submodules

log = logging.getLogger("agenttape.runtime")

SERVICES_PACKAGE = "agenttape.runtime.services"


class BackgroundService(Protocol):
    name: str

    async def run(self, stop: asyncio.Event) -> None:
        """Run until ``stop`` is set, then clean up and return."""
        ...


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    factory: Callable[[AppContext], BackgroundService]
    description: str
    #: Started by ``agenttape serve`` unless explicitly disabled.
    default_enabled: bool = True


_SERVICES: dict[str, ServiceSpec] = {}


def register_service(spec: ServiceSpec) -> None:
    _SERVICES[spec.name] = spec


def available_services() -> dict[str, ServiceSpec]:
    import_submodules(SERVICES_PACKAGE)
    return dict(sorted(_SERVICES.items()))


class Supervisor:
    """Runs background services as asyncio tasks and stops them together."""

    def __init__(self, ctx: AppContext, names: list[str]) -> None:
        specs = available_services()
        unknown = [n for n in names if n not in specs]
        if unknown:
            raise ValueError(f"unknown background services: {', '.join(unknown)}")
        self.ctx = ctx
        self.services = [specs[n].factory(ctx) for n in names]
        self.stop_event = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        for service in self.services:
            log.info("starting background service %s", service.name)
            self._tasks.append(asyncio.create_task(self._guard(service), name=service.name))

    async def _guard(self, service: BackgroundService) -> None:
        backoff = 1.0
        while not self.stop_event.is_set():
            try:
                await service.run(self.stop_event)
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "background service %s crashed; restarting in %.0fs", service.name, backoff
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.stop_event.wait(), timeout=backoff)
                backoff = min(backoff * 2, 60.0)

    async def stop(self, timeout: float = 30.0) -> None:
        self.stop_event.set()
        if not self._tasks:
            return
        _done, pending = await asyncio.wait(self._tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
