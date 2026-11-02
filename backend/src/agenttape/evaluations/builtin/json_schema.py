"""``json_schema`` evaluator: run outputs (or a JSON artifact) match a JSON Schema."""

from __future__ import annotations

import json
from typing import Any

import jsonschema

from agenttape.domain.enums import EvaluationStatus
from agenttape.domain.workflows import lookup
from agenttape.evaluations.base import (
    EvaluationContext,
    EvaluationOutcome,
    Evaluator,
    register_evaluator,
)
from agenttape.evaluations.isolated import run_isolated

MAX_ARTIFACT_BYTES = 2 * 1024 * 1024


@register_evaluator
class JsonSchemaEvaluator(Evaluator):
    type = "json_schema"
    version = "1"
    display_name = "JSON Schema"
    description = (
        "Validates the run's structured outputs (optionally a dotted path inside them) or a "
        "JSON artifact against a JSON Schema (draft 2020-12)."
    )

    def validate_config(self, config: dict[str, Any]) -> list[str]:
        schema = config.get("schema")
        if not isinstance(schema, dict):
            return ["'schema' must be a JSON Schema object"]
        try:
            jsonschema.Draft202012Validator.check_schema(schema)
        except jsonschema.SchemaError as exc:
            return [f"'schema' is not a valid JSON Schema: {exc.message}"]
        if config.get("source", "outputs") not in ("outputs", "artifact"):
            return ["'source' must be 'outputs' or 'artifact'"]
        if config.get("source") == "artifact" and not config.get("artifact_name"):
            return ["'artifact_name' is required when source is 'artifact'"]
        return []

    def config_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["schema"],
            "properties": {
                "schema": {"type": "object"},
                "source": {"enum": ["outputs", "artifact"], "default": "outputs"},
                "path": {"type": "string", "description": "Dotted path inside the outputs"},
                "artifact_name": {"type": "string"},
            },
        }

    async def evaluate(self, ctx: EvaluationContext, config: dict[str, Any]) -> EvaluationOutcome:
        source = config.get("source", "outputs")
        document: Any
        if source == "artifact":
            name = str(config["artifact_name"])
            artifacts = [a for a in await ctx.load_artifacts() if a.get("name") == name]
            if not artifacts:
                return EvaluationOutcome(
                    status=EvaluationStatus.FAIL,
                    explanation=f"No artifact named '{name}' was produced.",
                    evidence={"artifact_name": name},
                    score=0.0,
                )
            reader = ctx.extras.get("read_artifact")
            if not callable(reader):
                return EvaluationOutcome(
                    status=EvaluationStatus.SKIPPED,
                    explanation="Artifact contents are not available to this evaluation.",
                    evidence={"artifact_name": name},
                )
            try:
                document = json.loads(await reader(artifacts[-1]["id"], MAX_ARTIFACT_BYTES))
            except (ValueError, TypeError) as exc:
                return EvaluationOutcome(
                    status=EvaluationStatus.FAIL,
                    explanation=f"Artifact '{name}' is not valid JSON: {exc}",
                    evidence={"artifact_name": name},
                    score=0.0,
                )
        else:
            document = ctx.outputs or {}
            if config.get("path"):
                found = lookup(document, str(config["path"]))
                if (
                    not isinstance(found, dict | list | str | int | float | bool)
                    and found is not None
                ):
                    return EvaluationOutcome(
                        status=EvaluationStatus.FAIL,
                        explanation=f"The outputs have no value at '{config['path']}'.",
                        evidence={"path": config["path"], "output_keys": sorted(document)[:50]},
                        score=0.0,
                    )
                document = found
        # Schema ``pattern`` keywords are regular expressions run against agent output.
        errors: list[dict[str, str]] = await run_isolated(
            "json_schema", {"schema": config["schema"], "document": document}
        )
        if errors:
            return EvaluationOutcome(
                status=EvaluationStatus.FAIL,
                explanation=f"{len(errors)} schema violation(s): {errors[0]['message']}",
                evidence={"errors": errors},
                score=0.0,
            )
        return EvaluationOutcome(
            status=EvaluationStatus.PASS,
            explanation="The document matches the schema.",
            evidence={"source": source, "path": config.get("path")},
            score=1.0,
        )
