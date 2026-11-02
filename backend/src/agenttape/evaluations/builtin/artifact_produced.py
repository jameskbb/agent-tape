"""``artifact_produced`` evaluator: the run stored the expected artifact(s)."""

from __future__ import annotations

import fnmatch
from typing import Any

from agenttape.domain.enums import EvaluationStatus
from agenttape.evaluations.base import (
    EvaluationContext,
    EvaluationOutcome,
    Evaluator,
    register_evaluator,
)


@register_evaluator
class ArtifactProducedEvaluator(Evaluator):
    type = "artifact_produced"
    version = "1"
    display_name = "Artifact produced"
    description = "Passes when the run stored artifacts matching a name pattern, kind, and size."

    def validate_config(self, config: dict[str, Any]) -> list[str]:
        problems = []
        for key in ("name", "kind", "media_type"):
            if key in config and not isinstance(config[key], str):
                problems.append(f"'{key}' must be text")
        for key in ("min_count", "min_bytes"):
            value = config.get(key)
            if value is not None and (not isinstance(value, int) or value < 0):
                problems.append(f"'{key}' must be a non-negative integer")
        return problems

    def config_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Glob, e.g. *.md"},
                "kind": {"type": "string"},
                "media_type": {"type": "string"},
                "min_count": {"type": "integer", "minimum": 0, "default": 1},
                "min_bytes": {"type": "integer", "minimum": 0},
            },
        }

    async def evaluate(self, ctx: EvaluationContext, config: dict[str, Any]) -> EvaluationOutcome:
        artifacts = await ctx.load_artifacts()
        pattern = config.get("name")
        matching = [
            a
            for a in artifacts
            if (not pattern or fnmatch.fnmatchcase(str(a.get("name")), str(pattern)))
            and (not config.get("kind") or a.get("kind") == config["kind"])
            and (
                not config.get("media_type")
                or str(a.get("media_type", "")).startswith(str(config["media_type"]))
            )
            and int(a.get("size_bytes") or 0) >= int(config.get("min_bytes") or 0)
        ]
        need = int(config.get("min_count", 1))
        passed = len(matching) >= need
        return EvaluationOutcome(
            status=EvaluationStatus.PASS if passed else EvaluationStatus.FAIL,
            explanation=f"{len(matching)} matching artifact(s) of {len(artifacts)} stored"
            + ("" if passed else f" (needed {need})"),
            evidence={
                "matching": [
                    {k: a.get(k) for k in ("id", "name", "kind", "size_bytes")}
                    for a in matching[:20]
                ],
                "criteria": {k: config.get(k) for k in ("name", "kind", "media_type", "min_bytes")},
            },
            score=1.0 if passed else 0.0,
        )
