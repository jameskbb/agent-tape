"""``duration`` evaluator: the run finished within a time budget."""

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
class DurationEvaluator(Evaluator):
    type = "duration"
    version = "1"
    display_name = "Duration threshold"
    description = "Fails above max_seconds; warns above warn_seconds."

    def validate_config(self, config: dict[str, Any]) -> list[str]:
        problems = []
        for key in ("max_seconds", "warn_seconds"):
            value = config.get(key)
            if value is not None and (not isinstance(value, int | float) or value <= 0):
                problems.append(f"'{key}' must be a positive number")
        if config.get("max_seconds") is None and config.get("warn_seconds") is None:
            problems.append("set 'max_seconds' and/or 'warn_seconds'")
        return problems

    def config_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "max_seconds": {"type": "number", "exclusiveMinimum": 0},
                "warn_seconds": {"type": "number", "exclusiveMinimum": 0},
            },
        }

    async def evaluate(self, ctx: EvaluationContext, config: dict[str, Any]) -> EvaluationOutcome:
        if ctx.duration_ms is None:
            return EvaluationOutcome(
                status=EvaluationStatus.SKIPPED,
                explanation="The run's duration is not known yet.",
            )
        seconds = ctx.duration_ms / 1000
        evidence = {
            "duration_seconds": round(seconds, 3),
            "max_seconds": config.get("max_seconds"),
            "warn_seconds": config.get("warn_seconds"),
        }
        limit = config.get("max_seconds")
        if limit is not None and seconds > float(limit):
            return EvaluationOutcome(
                status=EvaluationStatus.FAIL,
                explanation=f"Took {seconds:.1f}s (limit {float(limit):g}s)",
                evidence=evidence,
                score=0.0,
            )
        warn = config.get("warn_seconds")
        if warn is not None and seconds > float(warn):
            return EvaluationOutcome(
                status=EvaluationStatus.WARNING,
                explanation=f"Took {seconds:.1f}s (warning above {float(warn):g}s)",
                evidence=evidence,
            )
        return EvaluationOutcome(
            status=EvaluationStatus.PASS,
            explanation=f"Took {seconds:.1f}s",
            evidence=evidence,
            score=1.0,
        )
