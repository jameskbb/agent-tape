"""Generic Command adapter (``generic-command``).

Runs one allowlisted local executable with an argument array. There is no shell and no
string interpolation: ``args`` are passed exactly as stored. The rendered prompt reaches
the process by one of ``prompt_mode``:

* ``stdin`` (default) - written to standard input;
* ``env``   - in the ``AGENTTAPE_PROMPT`` environment variable;
* ``arg``   - appended as the final argument;
* ``none``  - not passed.

Output is parsed per ``parser``:

* ``text`` (default) - stdout/stderr lines become ``stdout``/``stderr`` events;
* ``jsonl`` - each stdout line that is a JSON object with ``"agenttape": 1`` is an
  AgentTape event record (see docs/integrations/generic-command.md); other lines stay
  plain stdout.
"""

from __future__ import annotations

import json
from typing import Any

from agenttape.adapters.base import (
    AdapterResult,
    AgentAdapter,
    DiscoveryResult,
    ExecutionContext,
    ProfileIssue,
)
from agenttape.adapters.builtin._command_policy import judge
from agenttape.adapters.builtin._process_cli import (
    COMMON_SCHEMA,
    ExecutionSetupError,
    child_env,
    gate_commands,
    process_failure,
    resolve_launch,
    resolve_workdir,
    run_aux_commands,
    validate_policy,
    validate_shape,
)
from agenttape.adapters.policy import ProfilePolicy
from agenttape.adapters.process import CommandSpec, run_process
from agenttape.adapters.registry import register_adapter
from agenttape.domain.enums import AdapterAvailability, EventType, FailureCategory, Severity
from agenttape.domain.events import EventDraft
from agenttape.domain.policy import Verdict
from agenttape.domain.usage import merge_usage

PROMPT_MODES = ("stdin", "env", "arg", "none")
PARSERS = ("text", "jsonl")

#: Event types a generic command may emit through the JSONL protocol.
JSONL_TYPES = {
    EventType.AGENT_MESSAGE,
    EventType.TOOL_CALL,
    EventType.TOOL_RESULT,
    EventType.FILE_READ,
    EventType.FILE_WRITE,
    EventType.USAGE,
    EventType.WARNING,
    EventType.REASONING_SUMMARY,
}
SEVERITIES = {s.value for s in Severity}


def _prompt_secret_refs(ctx: ExecutionContext) -> list[str]:
    """Secret reference names the run's prompt resolves (from the run snapshot)."""
    prompt = (ctx.run.snapshot or {}).get("prompt") or {}
    refs = prompt.get("secret_refs") if isinstance(prompt, dict) else None
    return [str(r) for r in refs or [] if r]


@register_adapter
class GenericCommandAdapter(AgentAdapter):
    name = "generic-command"
    display_name = "Generic Command"
    description = (
        "Runs an allowlisted local executable with an argument array (never a shell). "
        "Captures stdout/stderr, or structured events from the AgentTape JSONL protocol."
    )
    capabilities = frozenset({"stdout_stderr", "structured_events_optional", "argv_only"})

    def discover(self) -> DiscoveryResult:
        return DiscoveryResult(
            adapter=self.name,
            status=AdapterAvailability.READY,
            version="1",
            executable="per profile",
            message="Built in. Each profile names an allowlisted executable.",
        )

    def validate_profile(self, config: dict[str, Any]) -> list[ProfileIssue]:
        issues = validate_shape(config)
        if config.get("executable") is None:
            issues.append(ProfileIssue("executable", "is required"))
        if config.get("prompt_mode", "stdin") not in PROMPT_MODES:
            issues.append(ProfileIssue("prompt_mode", f"must be one of: {', '.join(PROMPT_MODES)}"))
        if config.get("parser", "text") not in PARSERS:
            issues.append(ProfileIssue("parser", f"must be one of: {', '.join(PARSERS)}"))
        codes = config.get("success_exit_codes")
        if codes is not None and (
            not isinstance(codes, list) or not all(isinstance(c, int) for c in codes)
        ):
            issues.append(ProfileIssue("success_exit_codes", "must be a list of integers"))
        return issues

    def validate_with_policy(
        self, config: dict[str, Any], policy: ProfilePolicy
    ) -> list[ProfileIssue]:
        return validate_policy(config, policy, default_executable=None)

    def config_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["executable"],
            "properties": {
                **COMMON_SCHEMA,
                "prompt_mode": {"type": "string", "enum": list(PROMPT_MODES), "default": "stdin"},
                "parser": {"type": "string", "enum": list(PARSERS), "default": "text"},
                "success_exit_codes": {"type": "array", "items": {"type": "integer"}},
            },
        }

    def default_config(self) -> dict[str, Any]:
        return {"executable": "", "args": [], "prompt_mode": "stdin", "parser": "text"}

    # ------------------------------------------------------------------ normalization

    def normalize(self, raw: dict[str, Any]) -> list[EventDraft]:
        """Map one AgentTape JSONL record (``{"agenttape": 1, "type": ...}``) to an event."""
        try:
            etype = EventType(str(raw.get("type")))
        except ValueError:
            etype = None
        if etype is None or etype not in JSONL_TYPES:
            return [
                EventDraft(
                    EventType.WARNING,
                    f"Unsupported JSONL record type '{raw.get('type')}'",
                    {"raw": raw},
                    severity=Severity.WARNING,
                    source=self.name,
                )
            ]
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
        assert isinstance(payload, dict)
        summary = str(raw.get("summary") or payload.get("text") or etype.value)[:500]
        severity = str(raw.get("severity") or "info")
        usage = None
        if etype == EventType.USAGE:
            usage = {
                k: v
                for k, v in payload.items()
                if isinstance(v, int | float) and not isinstance(v, bool)
            } or None
        if etype == EventType.REASONING_SUMMARY:
            payload = {**payload, "provided_by": "generic-command"}
        return [
            EventDraft(
                etype,
                summary,
                payload,
                severity=severity if severity in SEVERITIES else "info",
                source=self.name,
                usage=usage,
            )
        ]

    # ------------------------------------------------------------------ execution

    async def execute(self, ctx: ExecutionContext) -> AdapterResult:
        cfg = ctx.profile
        try:
            exe = resolve_launch(ctx, default_executable=None)
            cwd = resolve_workdir(ctx)
        except ExecutionSetupError as exc:
            await ctx.events.event(EventType.ERROR, exc.message, {}, severity=Severity.ERROR)
            return exc.result()

        mode = cfg.get("prompt_mode", "stdin")
        args = [str(a) for a in cfg.get("args") or []]
        if mode == "arg" and _prompt_secret_refs(ctx):
            # argv is visible to every local user (ps, /proc/<pid>/cmdline): a resolved
            # secret must never be put there.
            message = (
                "This prompt uses secret references, and prompt_mode 'arg' would put their "
                "values on the command line where other local users can read them. Use "
                "prompt_mode 'stdin' or 'env', or pass the secret through secret_env."
            )
            await ctx.events.event(EventType.ERROR, message, {}, severity=Severity.ERROR)
            return AdapterResult.failure(FailureCategory.CONFIG_ERROR, message)
        # Judge the stored form (secret markers, never values) so approvals and events
        # never contain secret values.
        stored_args = [*args, ctx.prompt or ""] if mode == "arg" else args
        gated = await gate_commands(ctx, [str(exe), *stored_args], cwd)
        if gated is not None:
            return gated

        preflight = await run_aux_commands(
            ctx, cfg.get("preflight_commands") or [], label="preflight", cwd=cwd
        )
        if preflight is not None:
            return preflight

        prompt = ctx.resolved_prompt() if mode != "none" else None
        extra_env: dict[str, str] = {}
        stdin: bytes | None = None
        display = " ".join([exe.name, *args])[:300]
        if mode == "stdin":
            stdin = (prompt or "").encode()
        elif mode == "env":
            extra_env["AGENTTAPE_PROMPT"] = prompt or ""
        elif mode == "arg":
            args.append(prompt or "")
            display += " <prompt>"
            # The resolved prompt may differ from the stored one only by secret values;
            # re-check the real argv for hard-disallowed actions without recording it.
            if judge([str(exe), *args], cwd, elevated=False).verdict == Verdict.DENY:
                return AdapterResult.failure(
                    FailureCategory.POLICY_VIOLATION,
                    "The resolved command is refused by the V1 safety policy",
                )
        spec = CommandSpec(
            argv=[str(exe), *args],
            cwd=cwd,
            env=child_env(ctx, extra_env),
            stdin=stdin,
            display=display,
        )

        usage: dict[str, Any] | None = None
        on_stdout = None
        if cfg.get("parser", "text") == "jsonl":

            async def handle(line: str) -> bool:
                nonlocal usage
                stripped = line.strip()
                if not stripped.startswith("{"):
                    return False
                try:
                    record = json.loads(stripped)
                except ValueError:
                    return False
                if not isinstance(record, dict) or record.get("agenttape") != 1:
                    return False
                for draft in self.normalize(record):
                    if draft.usage:
                        usage = merge_usage(usage, draft.usage)
                    await ctx.events.emit(draft)
                return True

            on_stdout = handle

        result = await run_process(spec, ctx, on_stdout=on_stdout)
        success_codes = cfg.get("success_exit_codes") or [0]
        failure: AdapterResult | None = None
        if result.exit_code not in success_codes or result.stopped_reason is not None:
            failure = process_failure(result, exe.name) or AdapterResult.failure(
                FailureCategory.ADAPTER_ERROR,
                f"{exe.name} exited with code {result.exit_code}",
                exit_code=result.exit_code,
            )
        if failure is not None:
            failure.usage = usage
            return failure

        validation = await run_aux_commands(
            ctx, cfg.get("validation_commands") or [], label="validation", cwd=cwd
        )
        if validation is not None:
            return validation
        return AdapterResult.success(
            exit_code=result.exit_code,
            usage=usage,
            outputs={
                "exit_code": result.exit_code,
                "stdout_bytes": result.stdout_bytes,
                "stderr_bytes": result.stderr_bytes,
            },
        )
