"""``exit_code`` evaluator: passes when the attempt's exit code is in the expected set."""

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
class ExitCodeEvaluator(Evaluator):
    type = "exit_code"
    version = "1"
    display_name = "Exit code"
    description = "Passes when the process exit code is one of the expected codes (default 0)."

    def _expected(self, config: dict[str, Any]) -> list[int]:
        raw = config.get("expected", [0])
        values = raw if isinstance(raw, list) else [raw]
        return [int(v) for v in values]

    def validate_config(self, config: dict[str, Any]) -> list[str]:
        try:
            expected = self._expected(config)
        except (TypeError, ValueError):
            return ["'expected' must be an integer or a list of integers"]
        return [] if expected else ["'expected' must not be empty"]

    def config_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "expected": {
                    "oneOf": [
                        {"type": "integer"},
                        {"type": "array", "items": {"type": "integer"}, "minItems": 1},
                    ],
                    "default": 0,
                }
            },
        }

    async def evaluate(self, ctx: EvaluationContext, config: dict[str, Any]) -> EvaluationOutcome:
        expected = self._expected(config)
        if ctx.exit_code is None:
            return EvaluationOutcome(
                status=EvaluationStatus.SKIPPED,
                explanation="No exit code was reported for this attempt.",
                evidence={"expected": expected, "actual": None},
            )
        passed = ctx.exit_code in expected
        return EvaluationOutcome(
            status=EvaluationStatus.PASS if passed else EvaluationStatus.FAIL,
            explanation=(
                f"Exit code {ctx.exit_code} "
                + ("is" if passed else "is not")
                + f" in the expected set {expected}."
            ),
            evidence={"expected": expected, "actual": ctx.exit_code},
            score=1.0 if passed else 0.0,
        )
