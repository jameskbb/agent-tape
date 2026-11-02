"""``agent_judge`` evaluator: optional agent-as-judge, disabled unless a judge is configured.

AgentTape never fakes a judgement. This module defines the interface a real judge backend
implements (:class:`JudgeBackend`) and a registry for it. No backend ships in V1: the
Demo Agent is scripted and must not pose as a judge, and the real CLI adapters would need
credentials and paid usage. Until a backend is registered, every evaluation is
``skipped`` with an explanation, and the evaluator is listed as unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from agenttape.domain.enums import EvaluationStatus
from agenttape.evaluations.base import (
    EvaluationContext,
    EvaluationOutcome,
    Evaluator,
    register_evaluator,
)

if TYPE_CHECKING:
    from agenttape.application.context import AppContext


@dataclass
class JudgeVerdict:
    status: EvaluationStatus
    explanation: str
    score: float | None = None
    evidence: dict[str, Any] | None = None


class JudgeBackend(Protocol):
    name: str

    def available(self) -> str | None:
        """``None`` when usable, otherwise why not (shown to the user)."""
        ...

    async def judge(
        self, ctx: EvaluationContext, rubric: str, config: dict[str, Any]
    ) -> JudgeVerdict: ...


_BACKENDS: dict[str, JudgeBackend] = {}


def register_judge_backend(backend: JudgeBackend) -> None:
    _BACKENDS[backend.name] = backend


DISABLED = "Agent-as-judge is disabled: no judge adapter is configured, so no judgement was made."


@register_evaluator
class AgentJudgeEvaluator(Evaluator):
    type = "agent_judge"
    version = "1"
    display_name = "Agent as judge (optional)"
    description = (
        "Asks a configured judge adapter to grade the run against a rubric. Disabled unless "
        "a judge backend is configured; never simulated."
    )
    deterministic = False

    def availability(self, ctx: AppContext | None = None) -> str | None:
        usable = [b for b in _BACKENDS.values() if b.available() is None]
        return None if usable else DISABLED

    def validate_config(self, config: dict[str, Any]) -> list[str]:
        if not isinstance(config.get("rubric"), str) or not config["rubric"].strip():
            return ["'rubric' is required"]
        return []

    def config_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["rubric"],
            "properties": {
                "rubric": {"type": "string"},
                "backend": {"type": "string", "description": "Judge backend name"},
            },
        }

    async def evaluate(self, ctx: EvaluationContext, config: dict[str, Any]) -> EvaluationOutcome:
        name = config.get("backend")
        candidates = [_BACKENDS[name]] if name in _BACKENDS else list(_BACKENDS.values())
        backend = next((b for b in candidates if b.available() is None), None)
        if backend is None:
            return EvaluationOutcome(
                status=EvaluationStatus.SKIPPED,
                explanation=DISABLED,
                evidence={"configured_backends": sorted(_BACKENDS), "judge": None},
            )
        verdict = await backend.judge(ctx, str(config["rubric"]), config)
        return EvaluationOutcome(
            status=verdict.status,
            explanation=verdict.explanation,
            evidence={"judge": backend.name, **(verdict.evidence or {})},
            score=verdict.score,
        )
