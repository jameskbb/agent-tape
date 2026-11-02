"""``git_diff`` evaluator: diff size limits and the protected-path policy.

Reads the run's recorded file changes (``file_changes`` rows and ``git_change`` events).
When the run recorded no change data at all the result is ``skipped`` - it is not
possible to tell "changed nothing" from "changes were not captured".
"""

from __future__ import annotations

from typing import Any

from agenttape.domain.enums import EvaluationStatus
from agenttape.domain.policy import matches_protected
from agenttape.evaluations.base import (
    EvaluationContext,
    EvaluationOutcome,
    Evaluator,
    register_evaluator,
)
from agenttape.evaluations.builtin._common import project_settings


def _int_or_none(value: Any) -> int | None:
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


@register_evaluator
class GitDiffEvaluator(Evaluator):
    type = "git_diff"
    version = "1"
    display_name = "Git diff size and protected paths"
    description = (
        "Checks the recorded change set: maximum files, maximum changed lines, and whether "
        "any protected path was touched."
    )

    def validate_config(self, config: dict[str, Any]) -> list[str]:
        problems = []
        for key in ("max_files", "max_lines"):
            value = config.get(key)
            if value is not None and (_int_or_none(value) is None or value < 0):
                problems.append(f"'{key}' must be a non-negative integer")
        protected = config.get("protected_paths")
        if protected is not None and not (
            isinstance(protected, list) and all(isinstance(p, str) for p in protected)
        ):
            problems.append("'protected_paths' must be a list of glob patterns")
        return problems

    def config_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "max_files": {"type": "integer", "minimum": 0},
                "max_lines": {"type": "integer", "minimum": 0},
                "protected_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Defaults to the project's protected paths",
                },
                "protected_is_failure": {"type": "boolean", "default": True},
            },
        }

    async def evaluate(self, ctx: EvaluationContext, config: dict[str, Any]) -> EvaluationOutcome:
        changes = await ctx.load_file_changes()
        captured = await ctx.load_events(["git_change"])
        if not changes and not captured:
            return EvaluationOutcome(
                status=EvaluationStatus.SKIPPED,
                explanation="No file-change data was recorded for this run, so the diff could not be checked.",
                evidence={"file_changes": 0},
            )
        patterns = config.get("protected_paths")
        if patterns is None:
            patterns = list(project_settings(ctx).get("protected_paths") or [])
        files = len(changes)
        lines = sum((c.get("insertions") or 0) + (c.get("deletions") or 0) for c in changes)
        unknown_lines = sum(
            1 for c in changes if c.get("insertions") is None and not c.get("is_binary")
        )
        protected = sorted(
            {
                c["path"]
                for c in changes
                if c.get("is_protected") or matches_protected(c["path"], patterns)
            }
        )
        problems: list[str] = []
        max_files = _int_or_none(config.get("max_files"))
        max_lines = _int_or_none(config.get("max_lines"))
        if max_files is not None and files > max_files:
            problems.append(f"{files} files changed (limit {max_files})")
        if max_lines is not None and lines > max_lines:
            problems.append(f"{lines} lines changed (limit {max_lines})")
        protected_fails = bool(config.get("protected_is_failure", True))
        if protected and protected_fails:
            problems.append("protected paths touched: " + ", ".join(protected))
        evidence = {
            "files": files,
            "lines": lines,
            "files_without_line_counts": unknown_lines,
            "protected": protected,
            "patterns": patterns,
            "limits": {"max_files": max_files, "max_lines": max_lines},
        }
        if problems:
            return EvaluationOutcome(
                status=EvaluationStatus.FAIL,
                explanation="; ".join(problems),
                evidence=evidence,
                score=0.0,
            )
        if protected:
            return EvaluationOutcome(
                status=EvaluationStatus.WARNING,
                explanation="Protected paths were touched: " + ", ".join(protected),
                evidence=evidence,
            )
        return EvaluationOutcome(
            status=EvaluationStatus.PASS,
            explanation=f"{files} file(s), {lines} line(s) changed; no protected paths touched.",
            evidence=evidence,
            score=1.0,
        )
