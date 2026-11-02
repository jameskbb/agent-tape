"""``human_score`` evaluator: a person's score and notes.

Automatic evaluation cannot produce a human judgement, so when this evaluator runs as part
of a run or gate it reports ``skipped`` ("waiting for a person"). People record scores with
``POST /api/v1/runs/{id}/evaluations/human`` (the run's Evaluations tab), which stores a
``human_score`` result with the reviewer, score, and notes.
"""

from __future__ import annotations

from typing import Any

from agenttape.domain.enums import EvaluationStatus
from agenttape.evaluations.base import (
    EvaluationContext,
    EvaluationOutcome,
    Evaluator,
    register_evaluator,
)


@register_evaluator
class HumanScoreEvaluator(Evaluator):
    type = "human_score"
    version = "1"
    display_name = "Human score and notes"
    description = "A person scores the run (with notes) from the run's Evaluations tab."
    deterministic = False

    def validate_config(self, config: dict[str, Any]) -> list[str]:
        max_score = config.get("max_score", 5)
        if not isinstance(max_score, int | float) or max_score <= 0:
            return ["'max_score' must be a positive number"]
        threshold = config.get("pass_threshold")
        if threshold is not None and (not isinstance(threshold, int | float) or threshold < 0):
            return ["'pass_threshold' must be a non-negative number"]
        return []

    def config_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "max_score": {"type": "number", "default": 5},
                "pass_threshold": {"type": "number"},
            },
        }

    async def evaluate(self, ctx: EvaluationContext, config: dict[str, Any]) -> EvaluationOutcome:
        return EvaluationOutcome(
            status=EvaluationStatus.SKIPPED,
            explanation="Waiting for a person: record a score from the run's Evaluations tab.",
            evidence={"max_score": config.get("max_score", 5), "source": "human"},
        )
