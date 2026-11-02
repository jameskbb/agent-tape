"""Adapter registry. Every module in ``agenttape/adapters/builtin/`` is imported and each
class decorated with :func:`register_adapter` becomes available by its ``name``."""

from __future__ import annotations

from typing import TypeVar

from agenttape.adapters.base import AgentAdapter, DiscoveryResult
from agenttape.domain.enums import AdapterAvailability
from agenttape.domain.errors import NotFoundError
from agenttape.plugins import import_submodules

BUILTIN_PACKAGE = "agenttape.adapters.builtin"

_REGISTERED: dict[str, type[AgentAdapter]] = {}

A = TypeVar("A", bound=type[AgentAdapter])


def register_adapter(cls: A) -> A:
    """Class decorator: register an adapter class under ``cls.name``."""
    name = getattr(cls, "name", None)
    if not name:
        raise ValueError(f"{cls.__name__} must define a non-empty 'name'")
    existing = _REGISTERED.get(name)
    if existing is not None and existing is not cls:
        raise ValueError(f"adapter name '{name}' already registered by {existing.__name__}")
    _REGISTERED[name] = cls
    return cls


class AdapterRegistry:
    def __init__(self, adapters: dict[str, AgentAdapter] | None = None) -> None:
        self._adapters: dict[str, AgentAdapter] = dict(adapters or {})

    @classmethod
    def load(cls) -> AdapterRegistry:
        import_submodules(BUILTIN_PACKAGE)
        return cls({name: klass() for name, klass in sorted(_REGISTERED.items())})

    def register(self, adapter: AgentAdapter) -> None:
        self._adapters[adapter.name] = adapter

    def names(self) -> list[str]:
        return sorted(self._adapters)

    def has(self, name: str) -> bool:
        return name in self._adapters

    def get(self, name: str) -> AgentAdapter:
        try:
            return self._adapters[name]
        except KeyError:
            raise NotFoundError("adapter", name) from None

    def all(self) -> list[AgentAdapter]:
        return [self._adapters[n] for n in self.names()]

    def discover_all(self) -> list[DiscoveryResult]:
        results: list[DiscoveryResult] = []
        for adapter in self.all():
            try:
                results.append(adapter.discover())
            except Exception as exc:  # discovery must never crash callers
                results.append(
                    DiscoveryResult(
                        adapter=adapter.name,
                        status=AdapterAvailability.MISCONFIGURED,
                        message=f"discovery raised {type(exc).__name__}: {exc}",
                    )
                )
        return results
