"""``file_absent`` evaluator: forbidden files are not present in the working directory."""

from __future__ import annotations

from typing import Any

from agenttape.evaluations.base import (
    EvaluationContext,
    EvaluationOutcome,
    Evaluator,
    register_evaluator,
)
from agenttape.evaluations.builtin._files import check_paths, paths_config_problems, paths_schema


@register_evaluator
class FileAbsentEvaluator(Evaluator):
    type = "file_absent"
    version = "1"
    display_name = "File absent"
    description = "Passes when none of the listed paths (or globs) exist in the working directory."
    deterministic = False

    def validate_config(self, config: dict[str, Any]) -> list[str]:
        return paths_config_problems(config)

    def config_schema(self) -> dict[str, Any]:
        return paths_schema()

    async def evaluate(self, ctx: EvaluationContext, config: dict[str, Any]) -> EvaluationOutcome:
        return check_paths(ctx, config, want=False)
