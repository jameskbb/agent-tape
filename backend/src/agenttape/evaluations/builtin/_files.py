"""Shared logic for the ``file_exists`` and ``file_absent`` evaluators."""

from __future__ import annotations

from typing import Any

from agenttape.domain.enums import EvaluationStatus
from agenttape.evaluations.base import EvaluationContext, EvaluationOutcome
from agenttape.evaluations.builtin._common import resolve_inside

MAX_MATCHES = 200


def paths_config_problems(config: dict[str, Any]) -> list[str]:
    paths = config.get("paths")
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
        return ["'paths' must be a non-empty list of relative paths or glob patterns"]
    bad = [p for p in paths if p.startswith("/") or ".." in p.split("/")]
    if bad:
        return [f"paths must stay inside the working directory: {', '.join(bad)}"]
    return []


def paths_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["paths"],
        "properties": {
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": "Relative paths or glob patterns (e.g. tests/**/*.py)",
            }
        },
    }


def check_paths(ctx: EvaluationContext, config: dict[str, Any], *, want: bool) -> EvaluationOutcome:
    patterns = [str(p) for p in config["paths"]]
    if ctx.workdir is None or not ctx.workdir.is_dir():
        return EvaluationOutcome(
            status=EvaluationStatus.SKIPPED,
            explanation="No working directory is available for this run.",
            evidence={"paths": patterns},
        )
    root = ctx.workdir.resolve()
    found: dict[str, list[str]] = {}
    for pattern in patterns:
        if any(ch in pattern for ch in "*?["):
            hits = []
            for match in root.glob(pattern):
                resolved = match.resolve()
                if resolved.is_relative_to(root):
                    hits.append(str(resolved.relative_to(root)))
                if len(hits) >= MAX_MATCHES:
                    break
            found[pattern] = sorted(hits)
        else:
            target = resolve_inside(root, pattern)
            found[pattern] = [pattern] if target is not None and target.exists() else []
    if want:
        missing = [p for p, hits in found.items() if not hits]
        ok = not missing
        explanation = (
            f"All {len(patterns)} path(s) exist." if ok else "Missing: " + ", ".join(missing)
        )
    else:
        present = [p for p, hits in found.items() if hits]
        ok = not present
        explanation = (
            f"None of the {len(patterns)} path(s) exist."
            if ok
            else "Present but should be absent: " + ", ".join(present)
        )
    return EvaluationOutcome(
        status=EvaluationStatus.PASS if ok else EvaluationStatus.FAIL,
        explanation=explanation,
        evidence={"workdir": str(root), "matches": found},
        score=1.0 if ok else 0.0,
    )
