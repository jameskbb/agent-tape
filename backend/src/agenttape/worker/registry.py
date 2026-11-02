"""Run executor registry: which code executes a claimed run, keyed by ``Run.kind``.

``agent`` is built in (:mod:`agenttape.worker.executors.agent`). The workflow engine adds
``workflow`` by dropping a module into ``agenttape/worker/executors/`` that decorates a
class with :func:`register_executor`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar, TypeVar

from agenttape.application.context import AppContext
from agenttape.application.ports import ClaimedItem
from agenttape.plugins import import_submodules

EXECUTORS_PACKAGE = "agenttape.worker.executors"


@dataclass
class WorkerRuntime:
    """What an executor gets from the worker hosting it."""

    ctx: AppContext
    worker_id: str


class RunExecutor(ABC):
    kind: ClassVar[str]

    @abstractmethod
    async def execute(self, runtime: WorkerRuntime, claim: ClaimedItem) -> None:
        """Execute the claimed run and complete the queue item (always, even on failure)."""


_REGISTERED: dict[str, type[RunExecutor]] = {}

X = TypeVar("X", bound=type[RunExecutor])


def register_executor(cls: X) -> X:
    kind = getattr(cls, "kind", None)
    if not kind:
        raise ValueError(f"{cls.__name__} must define 'kind'")
    _REGISTERED[kind] = cls
    return cls


def load_executors() -> dict[str, RunExecutor]:
    import_submodules(EXECUTORS_PACKAGE)
    return {kind: cls() for kind, cls in sorted(_REGISTERED.items())}
