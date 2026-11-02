"""``command`` evaluator: run a test suite or check command in the run's working directory.

The command is an argument array (never a shell) and goes through the same safety policy
as workflow command nodes. An evaluator cannot pause for a person, so a command that
would need an elevated-command approval is reported as an ``error`` instead of running.
"""

from __future__ import annotations

from typing import Any

from agenttape.domain.enums import EvaluationStatus
from agenttape.domain.policy import Verdict, check_command
from agenttape.evaluations.base import (
    EvaluationContext,
    EvaluationOutcome,
    Evaluator,
    register_evaluator,
)
from agenttape.evaluations.builtin._common import project_settings, resolve_inside, run_argv


@register_evaluator
class CommandEvaluator(Evaluator):
    type = "command"
    version = "1"
    display_name = "Command / test suite"
    description = (
        "Runs an allowlisted command (argument array) in the run's working directory and "
        "passes when it exits with an expected code."
    )
    deterministic = False  # depends on the live workspace

    def validate_config(self, config: dict[str, Any]) -> list[str]:
        argv = config.get("argv")
        problems: list[str] = []
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            problems.append("'argv' must be a non-empty list of strings")
        timeout = config.get("timeout_seconds", 300)
        if not isinstance(timeout, int | float) or not 0 < timeout <= 3600:
            problems.append("'timeout_seconds' must be between 0 and 3600")
        codes = config.get("expected_exit_codes", [0])
        if not isinstance(codes, list) or not all(isinstance(c, int) for c in codes):
            problems.append("'expected_exit_codes' must be a list of integers")
        return problems

    def config_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["argv"],
            "properties": {
                "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "cwd": {"type": "string", "description": "Relative to the run's working directory"},
                "timeout_seconds": {"type": "number", "default": 300},
                "expected_exit_codes": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "default": [0],
                },
            },
        }

    async def evaluate(self, ctx: EvaluationContext, config: dict[str, Any]) -> EvaluationOutcome:
        argv = [str(a) for a in config["argv"]]
        if ctx.workdir is None or not ctx.workdir.is_dir():
            return EvaluationOutcome(
                status=EvaluationStatus.SKIPPED,
                explanation="No working directory is available for this run, so the command was not run.",
                evidence={"argv": argv},
            )
        settings = project_settings(ctx)
        allowlist = settings.get("command_allowlist")
        decision = check_command(
            argv,
            allowlist=allowlist if isinstance(allowlist, list) else None,
            trusted_commands=[v for v in settings.get("validation_commands") or [] if v],
        )
        if decision.verdict != Verdict.ALLOW:
            return EvaluationOutcome(
                status=EvaluationStatus.ERROR,
                explanation=f"Command not run: {decision.reason}",
                evidence={"argv": argv, "policy": decision.as_dict()},
            )
        cwd = ctx.workdir
        if config.get("cwd"):
            inside = resolve_inside(ctx.workdir, str(config["cwd"]))
            if inside is None or not inside.is_dir():
                return EvaluationOutcome(
                    status=EvaluationStatus.ERROR,
                    explanation=f"'cwd' {config['cwd']!r} is not a directory inside the workdir",
                    evidence={"argv": argv},
                )
            cwd = inside
        result = await run_argv(
            argv,
            cwd=cwd,
            timeout=float(config.get("timeout_seconds", 300)),
            env_allowlist=list(settings.get("env_allowlist") or []),
        )
        evidence = {
            "argv": argv,
            "cwd": str(cwd),
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
            "stdout_tail": result.stdout[-4000:],
            "stderr_tail": result.stderr[-4000:],
            "timed_out": result.timed_out,
        }
        if result.error:
            return EvaluationOutcome(
                status=EvaluationStatus.ERROR,
                explanation=f"Could not start {argv[0]}: {result.error}",
                evidence=evidence,
            )
        if result.timed_out:
            return EvaluationOutcome(
                status=EvaluationStatus.FAIL,
                explanation=f"{argv[0]} did not finish within {config.get('timeout_seconds', 300)}s",
                evidence=evidence,
                score=0.0,
            )
        expected = list(config.get("expected_exit_codes") or [0])
        passed = result.exit_code in expected
        return EvaluationOutcome(
            status=EvaluationStatus.PASS if passed else EvaluationStatus.FAIL,
            explanation=f"{' '.join(argv)[:200]} exited with {result.exit_code}"
            + ("" if passed else f" (expected {expected})"),
            evidence=evidence,
            score=1.0 if passed else 0.0,
        )
