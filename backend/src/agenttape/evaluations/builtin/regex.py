"""``regex`` evaluator: a pattern must match (or must not match) recorded output."""

from __future__ import annotations

import json
import re
from typing import Any

from agenttape.domain.enums import EvaluationStatus
from agenttape.evaluations.base import (
    EvaluationContext,
    EvaluationOutcome,
    Evaluator,
    register_evaluator,
)
from agenttape.evaluations.builtin._common import MAX_TEXT_BYTES, text_of
from agenttape.evaluations.isolated import run_isolated

MAX_PATTERN = 1000
SOURCES = {
    "stdout": ["stdout"],
    "stderr": ["stderr"],
    "output": ["stdout", "stderr"],
    "agent_messages": ["agent_message"],
    "all": ["stdout", "stderr", "agent_message", "tool_result"],
}
FLAGS = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}


@register_evaluator
class RegexEvaluator(Evaluator):
    type = "regex"
    version = "1"
    display_name = "Regex match / absence"
    description = (
        "Searches recorded output (stdout, stderr, agent messages, or run outputs) for a "
        "pattern; mode 'match' requires it, mode 'absent' forbids it."
    )

    def validate_config(self, config: dict[str, Any]) -> list[str]:
        problems: list[str] = []
        pattern = config.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            problems.append("'pattern' is required")
        elif len(pattern) > MAX_PATTERN:
            problems.append(f"'pattern' must be at most {MAX_PATTERN} characters")
        else:
            try:
                re.compile(pattern)
            except re.error as exc:
                problems.append(f"'pattern' is not a valid regular expression: {exc}")
        if config.get("mode", "match") not in ("match", "absent"):
            problems.append("'mode' must be 'match' or 'absent'")
        source = config.get("source", "output")
        if source not in (*SOURCES, "outputs"):
            problems.append(f"'source' must be one of: {', '.join([*SOURCES, 'outputs'])}")
        flags = config.get("flags", "")
        if not isinstance(flags, str) or any(f not in FLAGS for f in flags):
            problems.append("'flags' may contain only i, m, s")
        return problems

    def config_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["pattern"],
            "properties": {
                "pattern": {"type": "string"},
                "mode": {"enum": ["match", "absent"], "default": "match"},
                "source": {"enum": [*SOURCES, "outputs"], "default": "output"},
                "flags": {"type": "string", "description": "Any of i, m, s"},
                "min_matches": {"type": "integer", "minimum": 1, "default": 1},
            },
        }

    async def evaluate(self, ctx: EvaluationContext, config: dict[str, Any]) -> EvaluationOutcome:
        source = str(config.get("source", "output"))
        if source == "outputs":
            text = json.dumps(ctx.outputs or {}, sort_keys=True, default=str)[:MAX_TEXT_BYTES]
        else:
            text = text_of(await ctx.load_events(SOURCES[source]))
        flags = 0
        for f in str(config.get("flags", "")):
            flags |= FLAGS[f]
        matches: list[str] = await run_isolated(
            "regex", {"pattern": str(config["pattern"]), "flags": flags, "text": text}
        )
        mode = config.get("mode", "match")
        need = int(config.get("min_matches", 1))
        passed = len(matches) >= need if mode == "match" else not matches
        explanation = (
            f"Found {len(matches)}{'+' if len(matches) >= 20 else ''} match(es) in {source}"
            + (f" (needed {need})" if mode == "match" and not passed else "")
            + ("; the pattern must be absent" if mode == "absent" and matches else "")
        )
        return EvaluationOutcome(
            status=EvaluationStatus.PASS if passed else EvaluationStatus.FAIL,
            explanation=explanation,
            evidence={
                "pattern": config["pattern"],
                "mode": mode,
                "source": source,
                "matches": matches,
                "searched_chars": len(text),
            },
            score=1.0 if passed else 0.0,
        )
