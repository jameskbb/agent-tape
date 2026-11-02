"""Demo seed registry.

Each module in ``agenttape/seeds/`` (other than this one) may define::

    ORDER = 20                      # lower runs first; core demo data is 10
    def seed(ctx: AppContext, report: SeedReport) -> None: ...

Seeds must be idempotent (look records up by name first) and must tag everything they
create with ``is_demo=True`` so ``agenttape reset-demo`` can remove exactly that data.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agenttape.application.context import AppContext
from agenttape.plugins import import_submodules

SEEDS_PACKAGE = "agenttape.seeds"


@dataclass
class SeedReport:
    created: dict[str, list[str]] = field(default_factory=dict)
    existing: dict[str, list[str]] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)
    #: False when seeding implicitly (e.g. before a "Start a demo run" click); seeds that
    #: create demo *runs* only do so on an explicit ``agenttape seed``.
    include_runs: bool = True
    #: Execute seeded runs in this process (CLI). The HTTP API only queues them so the
    #: request returns immediately and the worker executes them.
    execute_inline: bool = True

    def add(self, kind: str, name: str, *, created: bool) -> None:
        bucket = self.created if created else self.existing
        bucket.setdefault(kind, []).append(name)


SeedFn = Callable[[AppContext, SeedReport], None]


def load_seeds() -> list[tuple[int, str, SeedFn]]:
    seeds: list[tuple[int, str, SeedFn]] = []
    for module in import_submodules(SEEDS_PACKAGE):
        fn = getattr(module, "seed", None)
        if callable(fn) and module.__name__ != __name__:
            seeds.append((int(getattr(module, "ORDER", 50)), module.__name__, fn))
    return sorted(seeds, key=lambda s: (s[0], s[1]))
