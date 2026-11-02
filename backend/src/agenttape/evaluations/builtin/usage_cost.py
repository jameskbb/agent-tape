"""``usage_cost`` evaluator: token usage and cost limits - only when really reported.

Usage the adapter did not report is ``skipped`` with an "unavailable" explanation, and
simulated usage (the Demo Agent's, marked ``"simulated": true``) never counts as real:
it is skipped too. Cost is never estimated from tokens.
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

TOKEN_LIMITS = {
    "max_input_tokens": "input_tokens",
    "max_output_tokens": "output_tokens",
    "max_total_tokens": "total_tokens",
}


@register_evaluator
class UsageCostEvaluator(Evaluator):
    type = "usage_cost"
    version = "1"
    display_name = "Usage / cost threshold"
    description = (
        "Checks reported token usage and cost against limits. Unreported or simulated usage "
        "is skipped as unavailable, never treated as real."
    )

    def validate_config(self, config: dict[str, Any]) -> list[str]:
        keys = [*TOKEN_LIMITS, "max_cost"]
        problems = []
        for key in keys:
            value = config.get(key)
            if value is not None and (not isinstance(value, int | float) or value < 0):
                problems.append(f"'{key}' must be a non-negative number")
        if not any(config.get(k) is not None for k in keys):
            problems.append("set at least one limit: " + ", ".join(keys))
        return problems

    def config_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                **{k: {"type": "integer", "minimum": 0} for k in TOKEN_LIMITS},
                "max_cost": {
                    "type": "number",
                    "minimum": 0,
                    "description": "In the reported currency",
                },
            },
        }

    async def evaluate(self, ctx: EvaluationContext, config: dict[str, Any]) -> EvaluationOutcome:
        usage = dict(ctx.usage or {})
        cost = ctx.extras.get("cost")
        if not usage and cost is None:
            return EvaluationOutcome(
                status=EvaluationStatus.SKIPPED,
                explanation="Usage unavailable: the adapter did not report token usage or cost.",
                evidence={"usage": None},
            )
        if usage.get("simulated"):
            return EvaluationOutcome(
                status=EvaluationStatus.SKIPPED,
                explanation="Usage unavailable: the only usage reported is simulated (Demo Agent), "
                "which is never treated as real.",
                evidence={"simulated": True},
            )
        if "total_tokens" not in usage and (
            usage.get("input_tokens") is not None and usage.get("output_tokens") is not None
        ):
            usage["total_tokens"] = int(usage["input_tokens"]) + int(usage["output_tokens"])
        over: list[str] = []
        unknown: list[str] = []
        checked: dict[str, Any] = {}
        for key, field in TOKEN_LIMITS.items():
            limit = config.get(key)
            if limit is None:
                continue
            value = usage.get(field)
            if value is None:
                unknown.append(field)
                continue
            checked[field] = {"value": value, "limit": limit}
            if value > limit:
                over.append(f"{field} {value} > {limit}")
        if config.get("max_cost") is not None:
            amount = (cost or {}).get("amount") if isinstance(cost, dict) else None
            if amount is None:
                unknown.append("cost")
            else:
                checked["cost"] = {"value": amount, "limit": config["max_cost"]}
                if float(amount) > float(config["max_cost"]):
                    over.append(f"cost {amount} > {config['max_cost']}")
        evidence = {"checked": checked, "unavailable": unknown}
        if over:
            return EvaluationOutcome(
                status=EvaluationStatus.FAIL,
                explanation="Over budget: " + "; ".join(over),
                evidence=evidence,
                score=0.0,
            )
        if not checked:
            return EvaluationOutcome(
                status=EvaluationStatus.SKIPPED,
                explanation="Usage unavailable for the configured limits: " + ", ".join(unknown),
                evidence=evidence,
            )
        return EvaluationOutcome(
            status=EvaluationStatus.PASS,
            explanation="Within limits"
            + (f" (not reported: {', '.join(unknown)})" if unknown else ""),
            evidence=evidence,
            score=1.0,
        )
