"""Evaluator protocol and registry.

Evaluators are deterministic checks run after an attempt (or by a workflow evaluation
gate). Each returns pass/fail/warning/error/skipped with structured evidence. Modules in
``agenttape/evaluations/builtin/`` are auto-discovered; decorate a class with
:func:`register_evaluator`. See docs/EXTENDING.md ("Add an evaluator").
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, TypeVar

from agenttape.domain.enums import EvaluationStatus
from agenttape.domain.errors import NotFoundError
from agenttape.plugins import import_submodules

BUILTIN_PACKAGE = "agenttape.evaluations.builtin"


@dataclass
class EvaluationSpec:
    """A configured evaluator attached to a profile, a run, or a workflow node."""

    type: str
    name: str | None = None
    config: dict[str, Any] = field(default_factory=dict)
    required: bool = False
    definition_id: str | None = None
    definition_version_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvaluationSpec:
        return cls(
            type=str(data["type"]),
            name=data.get("name"),
            config=dict(data.get("config") or {}),
            required=bool(data.get("required", False)),
            definition_id=data.get("definition_id"),
            definition_version_id=data.get("definition_version_id"),
        )


@dataclass
class EvaluationContext:
    run_id: str
    attempt_id: str | None
    step_run_id: str | None
    exit_code: int | None
    outcome: str | None
    outputs: dict[str, Any]
    workdir: Path | None
    duration_ms: int | None
    usage: dict[str, Any] | None
    #: Loads the run's events (optionally filtered by type) from the database.
    load_events: Callable[[list[str] | None], Awaitable[list[dict[str, Any]]]]
    #: Loads the run's artifacts' metadata.
    load_artifacts: Callable[[], Awaitable[list[dict[str, Any]]]]
    #: Loads the run's recorded file changes.
    load_file_changes: Callable[[], Awaitable[list[dict[str, Any]]]]
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvaluationOutcome:
    status: EvaluationStatus
    explanation: str
    evidence: dict[str, Any] = field(default_factory=dict)
    score: float | None = None


class Evaluator(ABC):
    type: ClassVar[str]
    version: ClassVar[str] = "1"
    display_name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    #: True when results depend only on recorded data (safe to rerun).
    deterministic: ClassVar[bool] = True

    def validate_config(self, config: dict[str, Any]) -> list[str]:
        """Return human-readable problems with ``config`` (empty when valid)."""
        return []

    def config_schema(self) -> dict[str, Any]:
        return {}

    @abstractmethod
    async def evaluate(
        self, ctx: EvaluationContext, config: dict[str, Any]
    ) -> EvaluationOutcome: ...


_REGISTERED: dict[str, type[Evaluator]] = {}

E = TypeVar("E", bound=type[Evaluator])


def register_evaluator(cls: E) -> E:
    name = getattr(cls, "type", None)
    if not name:
        raise ValueError(f"{cls.__name__} must define a non-empty 'type'")
    existing = _REGISTERED.get(name)
    if existing is not None and existing is not cls:
        raise ValueError(f"evaluator type '{name}' already registered by {existing.__name__}")
    _REGISTERED[name] = cls
    return cls


class EvaluatorRegistry:
    def __init__(self, evaluators: dict[str, Evaluator] | None = None) -> None:
        self._evaluators = dict(evaluators or {})

    @classmethod
    def load(cls) -> EvaluatorRegistry:
        import_submodules(BUILTIN_PACKAGE)
        return cls({name: klass() for name, klass in sorted(_REGISTERED.items())})

    def get(self, type_: str) -> Evaluator:
        try:
            return self._evaluators[type_]
        except KeyError:
            raise NotFoundError("evaluator", type_) from None

    def has(self, type_: str) -> bool:
        return type_ in self._evaluators

    def all(self) -> list[Evaluator]:
        return [self._evaluators[k] for k in sorted(self._evaluators)]

    def register(self, evaluator: Evaluator) -> None:
        self._evaluators[evaluator.type] = evaluator
