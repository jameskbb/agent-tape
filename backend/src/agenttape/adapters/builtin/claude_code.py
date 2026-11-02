"""Claude Code adapter (``claude-code``).

Runs ``claude -p --output-format stream-json --verbose`` with the rendered prompt on
stdin (documented non-interactive mode; tested against Claude Code 2.1.283) and maps each
stream-json message to the event envelope. See docs/integrations/claude-code.md.

Mapping summary:

* ``system/init``            -> ``lifecycle`` (session id, model, tool names; no secrets)
* ``assistant`` text block   -> ``agent_message``
* ``assistant`` tool_use     -> ``tool_call`` (tool, input, tool_use_id)
* ``user`` tool_result       -> ``tool_result`` (``parent_event_id`` = the call)
* ``assistant`` thinking     -> dropped (hidden reasoning is never captured)
* ``system/api_retry``       -> ``warning``
* ``system/permission_denied`` -> ``warning``
* ``result``                 -> ``usage`` (usage and ``total_cost_usd`` exactly as reported)
* ``stream_event`` / others  -> dropped / ``debug`` passthrough
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agenttape.adapters.base import (
    AdapterResult,
    AgentAdapter,
    DiscoveryResult,
    ExecutionContext,
    ProfileIssue,
)
from agenttape.adapters.builtin._process_cli import (
    COMMON_SCHEMA,
    ExecutionSetupError,
    child_env,
    default_executable_for,
    discover_cli,
    gate_commands,
    is_str_list,
    process_failure,
    resolve_launch,
    resolve_workdir,
    run_aux_commands,
    validate_argv_list,
    validate_policy,
    validate_shape,
)
from agenttape.adapters.policy import ProfilePolicy
from agenttape.adapters.process import CommandSpec, run_process
from agenttape.adapters.registry import register_adapter
from agenttape.domain.enums import EventType, FailureCategory, Severity
from agenttape.domain.events import EventDraft
from agenttape.infrastructure.db.base import new_id
from agenttape.mcp.launch import (
    FINDINGS_PROMPT_CONTRACT,
    RECORD_FINDING_TOOL,
    launch_config,
    remove_launch_config,
    write_launch_config,
)
from agenttape.security.capture import mint_attempt_token, revoke_attempt_token

ENV_OVERRIDE = "AGENTTAPE_CLAUDE_EXECUTABLE"
DEFAULT_NAME = "claude"
TESTED_VERSION = "2.1.283"
PERMISSION_MODES = ("default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions")
#: Flags that would disable AgentTape's safety posture or break the stream contract.
FORBIDDEN_EXTRA = {
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "-p",
    "--print",
    "--output-format",
    "--input-format",
    "--bg",
    "--background",
    "--cloud",
    "--remote-control",
    "--tmux",
    "-w",
    "--worktree",
    "--ide",
    "--chrome",
}
USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
#: A stream-json line reaches the normalizer whole up to this size plus
#: ``adapters.process.REDACTION_OVERLAP_BYTES`` (4 KiB; ``run_process`` splits a line only
#: past that sum, and buffers up to one more read chunk before it does). A ``user`` record
#: carries a tool result's text and, in ``tool_use_result``, the file content again, both
#: JSON-escaped, so a Read up to the 1 MiB retained-content cap
#: (``findings/retained_output.py``) needs well over twice that. A longer line is split into
#: plain-output pieces and its tool result is not parsed.
STREAM_JSON_MAX_LINE_BYTES = 8 * 1024 * 1024
AUTH_ERRORS = {"authentication_failed", "oauth_org_not_allowed", "billing_error", "account_on_hold"}


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, dict):
                parts.append(f"[{block.get('type', 'content')}]")
        return "\n".join(parts)
    return "" if content is None else json.dumps(content)[:4000]


def _tool_target(name: str, args: Any) -> str:
    if not isinstance(args, dict):
        return ""
    for key in ("file_path", "path", "command", "pattern", "url", "description"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return value.splitlines()[0][:120]
    return ""


class ClaudeStreamNormalizer:
    """Stateful mapper for one attempt's stream (links tool results to their calls)."""

    def __init__(self, source: str = "claude-code") -> None:
        self.source = source
        self.tool_events: dict[str, str] = {}
        self.result: dict[str, Any] | None = None
        self.session_id: str | None = None
        self.auth_error: str | None = None
        self.denials: list[dict[str, Any]] = []

    def _draft(
        self, etype: EventType, summary: str, payload: dict[str, Any], **kw: Any
    ) -> EventDraft:
        return EventDraft(etype, summary[:500], payload, source=self.source, **kw)

    def feed(self, raw: dict[str, Any]) -> list[EventDraft]:
        kind = raw.get("type")
        sub = raw.get("subtype")
        if kind == "system" and sub == "init":
            self.session_id = raw.get("session_id") or self.session_id
            raw_tools = raw.get("tools")
            tools: list[Any] = raw_tools if isinstance(raw_tools, list) else []
            return [
                self._draft(
                    EventType.LIFECYCLE,
                    f"Claude Code session started (model {raw.get('model', 'unknown')})",
                    {
                        "phase": "session_started",
                        "session_id": raw.get("session_id"),
                        "model": raw.get("model"),
                        "cwd": raw.get("cwd"),
                        "permission_mode": raw.get("permissionMode"),
                        "tools": [str(t) for t in tools][:100],
                        "claude_code_version": raw.get("claude_code_version"),
                    },
                )
            ]
        if kind == "system" and sub == "api_retry":
            error = str(raw.get("error") or "unknown")
            if error in AUTH_ERRORS:
                self.auth_error = error
            return [
                self._draft(
                    EventType.WARNING,
                    f"Claude API request failed ({error}); retry {raw.get('attempt')} of "
                    f"{raw.get('max_retries')}",
                    {
                        "error": error,
                        "attempt": raw.get("attempt"),
                        "max_retries": raw.get("max_retries"),
                        "retry_delay_ms": raw.get("retry_delay_ms"),
                        "error_status": raw.get("error_status"),
                    },
                    severity=Severity.WARNING,
                )
            ]
        if kind == "system" and sub == "permission_denied":
            self.denials.append({k: v for k, v in raw.items() if k not in ("type", "subtype")})
            tool = raw.get("tool_name") or raw.get("tool") or "a tool"
            return [
                self._draft(
                    EventType.WARNING,
                    f"Permission denied for {tool}",
                    {k: v for k, v in raw.items() if k not in ("type", "subtype")},
                    severity=Severity.WARNING,
                )
            ]
        if kind == "assistant":
            return self._assistant(raw)
        if kind == "user":
            return self._user(raw)
        if kind == "result":
            return self._result(raw)
        if kind == "stream_event":
            return []
        return [
            self._draft(
                EventType.AGENT_MESSAGE,
                f"Claude Code record: {kind}{'/' + str(sub) if sub else ''}",
                {"role": "system", "raw_type": kind, "subtype": sub},
                severity=Severity.DEBUG,
            )
        ]

    def _assistant(self, raw: dict[str, Any]) -> list[EventDraft]:
        message = raw.get("message") if isinstance(raw.get("message"), dict) else {}
        assert isinstance(message, dict)
        parent_tool = raw.get("parent_tool_use_id")
        drafts: list[EventDraft] = []
        content = message.get("content")
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = str(block.get("text", ""))
                if text.strip():
                    drafts.append(
                        self._draft(
                            EventType.AGENT_MESSAGE,
                            text,
                            {
                                "role": "assistant",
                                "text": text,
                                "model": message.get("model"),
                                "subagent_of": parent_tool,
                            },
                        )
                    )
            elif btype == "tool_use":
                name = str(block.get("name", "tool"))
                args = block.get("input") if isinstance(block.get("input"), dict) else {}
                event_id = new_id()
                tool_use_id = str(block.get("id", ""))
                if tool_use_id:
                    self.tool_events[tool_use_id] = event_id
                target = _tool_target(name, args)
                drafts.append(
                    self._draft(
                        EventType.TOOL_CALL,
                        f"{name}({target})" if target else name,
                        {
                            "tool": name,
                            "input": args,
                            "tool_use_id": tool_use_id,
                            "subagent_of": parent_tool,
                        },
                        id=event_id,
                        parent_event_id=self.tool_events.get(str(parent_tool))
                        if parent_tool
                        else None,
                    )
                )
            # "thinking" / "redacted_thinking" blocks are deliberately dropped.
        return drafts

    def _user(self, raw: dict[str, Any]) -> list[EventDraft]:
        message = raw.get("message") if isinstance(raw.get("message"), dict) else {}
        assert isinstance(message, dict)
        content = message.get("content")
        drafts: list[EventDraft] = []
        if not isinstance(content, list):
            return drafts
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_use_id = str(block.get("tool_use_id", ""))
            output = _text_of(block.get("content"))
            is_error = bool(block.get("is_error"))
            first = output.strip().splitlines()[0][:200] if output.strip() else "(empty)"
            drafts.append(
                self._draft(
                    EventType.TOOL_RESULT,
                    ("Tool error: " if is_error else "Tool result: ") + first,
                    {"tool_use_id": tool_use_id, "output": output, "is_error": is_error},
                    severity=Severity.ERROR if is_error else Severity.INFO,
                    parent_event_id=self.tool_events.get(tool_use_id),
                )
            )
        return drafts

    def _result(self, raw: dict[str, Any]) -> list[EventDraft]:
        self.result = raw
        self.session_id = raw.get("session_id") or self.session_id
        usage = self.usage()
        cost = self.cost()
        parts = []
        if usage:
            parts.append(
                f"{usage.get('input_tokens', '?')} in / {usage.get('output_tokens', '?')} out tokens"
            )
        if cost:
            parts.append(f"${cost['total_usd']:.4f} (estimate reported by Claude Code)")
        summary = "Claude Code reported " + (", ".join(parts) if parts else "no usage")
        payload: dict[str, Any] = {
            **(usage or {}),
            "subtype": raw.get("subtype"),
            "is_error": raw.get("is_error"),
            "num_turns": raw.get("num_turns"),
            "duration_ms": raw.get("duration_ms"),
            "duration_api_ms": raw.get("duration_api_ms"),
            "session_id": raw.get("session_id"),
        }
        return [self._draft(EventType.USAGE, summary, payload, usage=usage, cost=cost)]

    def usage(self) -> dict[str, Any] | None:
        raw = (self.result or {}).get("usage")
        if not isinstance(raw, dict):
            return None
        out = {
            k: raw[k]
            for k in USAGE_KEYS
            if isinstance(raw.get(k), int | float) and not isinstance(raw.get(k), bool)
        }
        return out or None

    def cost(self) -> dict[str, Any] | None:
        value = (self.result or {}).get("total_cost_usd")
        if isinstance(value, int | float) and not isinstance(value, bool):
            return {
                "total_usd": float(value),
                "currency": "USD",
                "source": "claude-code total_cost_usd (client-side estimate)",
            }
        return None


@register_adapter
class ClaudeCodeAdapter(AgentAdapter):
    name = "claude-code"
    display_name = "Claude Code"
    description = (
        "Anthropic's Claude Code CLI in non-interactive mode (-p) with stream-json output: "
        "messages, tool calls and results, usage, and the CLI's own cost estimate."
    )
    capabilities = frozenset(
        {"structured_events", "tool_calls", "usage", "cost", "model_selection", "tool_rules"}
    )
    findings_capture = "mcp"

    def discover(self) -> DiscoveryResult:
        status, version, exe, message, details = discover_cli(
            self.name,
            env_override=ENV_OVERRIDE,
            default_name=DEFAULT_NAME,
            setup_hint="Install Claude Code (https://code.claude.com/docs) or set "
            f"{ENV_OVERRIDE} to its path.",
        )
        details["tested_version"] = TESTED_VERSION
        return DiscoveryResult(
            adapter=self.name,
            status=status,
            version=version,
            executable=exe,
            message=message,
            details=details,
        )

    def validate_profile(self, config: dict[str, Any]) -> list[ProfileIssue]:
        issues = validate_shape(config)
        mode = config.get("permission_mode")
        if mode is not None and mode not in PERMISSION_MODES:
            issues.append(
                ProfileIssue("permission_mode", f"must be one of: {', '.join(PERMISSION_MODES)}")
            )
        if mode == "bypassPermissions":
            issues.append(
                ProfileIssue(
                    "permission_mode",
                    "bypassPermissions skips every permission check; use only in a sandbox",
                    severity="warning",
                )
            )
        model = config.get("model")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            issues.append(ProfileIssue("model", "must be a model alias or full name"))
        turns = config.get("max_turns")
        if turns is not None and (not isinstance(turns, int) or not 1 <= turns <= 500):
            issues.append(ProfileIssue("max_turns", "must be an integer between 1 and 500"))
        for key in ("allowed_tools", "denied_tools"):
            if config.get(key) is not None and not is_str_list(config.get(key)):
                issues.append(ProfileIssue(key, "must be a list of tool rules"))
        issues.extend(validate_argv_list("extra_args", config.get("extra_args")))
        for arg in config.get("extra_args") or []:
            if isinstance(arg, str) and arg.split("=", 1)[0] in FORBIDDEN_EXTRA:
                issues.append(ProfileIssue("extra_args", f"{arg} is not allowed here"))
        budget = (config.get("budget") or {}).get("max_cost_usd")
        if budget is not None and (not isinstance(budget, int | float) or budget <= 0):
            issues.append(ProfileIssue("budget.max_cost_usd", "must be a positive number"))
        if config.get("args"):
            issues.append(
                ProfileIssue("args", "use extra_args for Claude Code flags", severity="warning")
            )
        return issues

    def validate_with_policy(
        self, config: dict[str, Any], policy: ProfilePolicy
    ) -> list[ProfileIssue]:
        exe, trusted = default_executable_for(DEFAULT_NAME, ENV_OVERRIDE)
        return validate_policy(config, policy, default_executable=exe, default_names=trusted)

    def config_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                **COMMON_SCHEMA,
                "model": {"type": "string", "description": "Alias (sonnet, opus) or full name"},
                "permission_mode": {"type": "string", "enum": list(PERMISSION_MODES)},
                "max_turns": {"type": "integer", "minimum": 1, "maximum": 500},
                "allowed_tools": {"type": "array", "items": {"type": "string"}},
                "denied_tools": {"type": "array", "items": {"type": "string"}},
                "bare": {"type": "boolean", "default": False},
                "extra_args": {"type": "array", "items": {"type": "string"}},
            },
        }

    def default_config(self) -> dict[str, Any]:
        return {"permission_mode": "default", "max_turns": 20}

    def build_argv(
        self, exe: str, config: dict[str, Any], *, mcp_config: Path | None = None
    ) -> list[str]:
        argv = [exe, "-p", "--output-format", "stream-json", "--verbose"]
        if config.get("bare"):
            argv.append("--bare")
        if mcp_config is not None:
            argv += [
                "--mcp-config",
                str(mcp_config),
                "--strict-mcp-config",
                "--append-system-prompt",
                FINDINGS_PROMPT_CONTRACT,
            ]
        if config.get("model"):
            argv += ["--model", str(config["model"])]
        if config.get("permission_mode"):
            argv += ["--permission-mode", str(config["permission_mode"])]
        if config.get("max_turns"):
            argv += ["--max-turns", str(int(config["max_turns"]))]
        allowed_tools = [str(t) for t in config.get("allowed_tools") or []]
        denied_tools = {str(t) for t in config.get("denied_tools") or []}
        denies_record_finding = bool(
            denied_tools & {RECORD_FINDING_TOOL, "mcp__agenttape", "mcp__agenttape__*"}
        )
        if (
            mcp_config is not None
            and not denies_record_finding
            and RECORD_FINDING_TOOL not in allowed_tools
        ):
            allowed_tools = [*allowed_tools, RECORD_FINDING_TOOL]
        if allowed_tools:
            argv += ["--allowedTools", ",".join(allowed_tools)]
        if config.get("denied_tools"):
            argv += ["--disallowedTools", ",".join(config["denied_tools"])]
        budget = (config.get("budget") or {}).get("max_cost_usd")
        if budget:
            argv += ["--max-budget-usd", f"{float(budget):g}"]
        argv += [str(a) for a in config.get("extra_args") or []]
        return argv

    def normalize(self, raw: dict[str, Any]) -> list[EventDraft]:
        return ClaudeStreamNormalizer(self.name).feed(raw)

    async def execute(self, ctx: ExecutionContext) -> AdapterResult:
        cfg = ctx.profile
        try:
            default, trusted = default_executable_for(DEFAULT_NAME, ENV_OVERRIDE)
            exe = resolve_launch(ctx, default_executable=default, default_names=trusted)
            cwd = resolve_workdir(ctx)
        except ExecutionSetupError as exc:
            await ctx.events.event(EventType.ERROR, exc.message, {}, severity=Severity.ERROR)
            return exc.result()

        capture_api_base = ctx.limits.get("capture_api_base")
        mcp_config_path: Path | None = None
        minted_token = False
        if capture_api_base:
            token = mint_attempt_token(ctx.data_dir, ctx.attempt.id)
            minted_token = True
            mcp_config_path = write_launch_config(
                ctx.data_dir,
                ctx.attempt.id,
                launch_config(
                    api_base=str(capture_api_base),
                    run_id=ctx.run.id,
                    attempt_id=ctx.attempt.id,
                    token=token,
                    data_dir=ctx.data_dir,
                ),
            )
        else:
            await ctx.events.event(
                EventType.WARNING,
                "AgentTape MCP server not configured for this attempt (no API address); "
                "record_finding is unavailable",
                {},
                severity=Severity.WARNING,
            )

        try:
            argv = self.build_argv(str(exe), cfg, mcp_config=mcp_config_path)
            gated = await gate_commands(ctx, argv, cwd)
            if gated is not None:
                return gated
            preflight = await run_aux_commands(
                ctx, cfg.get("preflight_commands") or [], label="preflight", cwd=cwd
            )
            if preflight is not None:
                return preflight

            normalizer = ClaudeStreamNormalizer(self.name)

            async def on_line(line: str) -> bool:
                stripped = line.strip()
                if not stripped.startswith("{"):
                    return False
                try:
                    record = json.loads(stripped)
                except ValueError:
                    return False
                if not isinstance(record, dict):
                    return False
                for draft in normalizer.feed(record):
                    await ctx.events.emit(draft)
                return True

            spec = CommandSpec(
                argv=argv,
                cwd=cwd,
                env=child_env(ctx),
                stdin=(ctx.resolved_prompt() or "").encode(),
                display=" ".join([exe.name, *argv[1:]])[:300],
            )
            proc = await run_process(
                spec, ctx, on_stdout=on_line, max_stdout_line_bytes=STREAM_JSON_MAX_LINE_BYTES
            )
            usage, cost = normalizer.usage(), normalizer.cost()
            outputs: dict[str, Any] = {"session_id": normalizer.session_id}
            result = normalizer.result or {}
            if result:
                outputs.update(
                    {
                        "result": result.get("result"),
                        "subtype": result.get("subtype"),
                        "num_turns": result.get("num_turns"),
                    }
                )
            failure = process_failure(proc, "Claude Code")
            if failure is None and result.get("is_error"):
                failure = AdapterResult.failure(
                    FailureCategory.ADAPTER_ERROR,
                    f"Claude Code finished with an error result ({result.get('subtype')})",
                    exit_code=proc.exit_code,
                )
            if failure is None and not result:
                failure = AdapterResult.failure(
                    FailureCategory.ADAPTER_ERROR,
                    "Claude Code exited without a result message",
                    exit_code=proc.exit_code,
                )
            if failure is not None:
                if (
                    normalizer.auth_error
                    and failure.failure_category == FailureCategory.ADAPTER_ERROR
                ):
                    failure.failure_category = FailureCategory.CONFIG_ERROR
                    failure.message = (
                        f"Claude Code could not authenticate ({normalizer.auth_error})"
                    )
                failure.usage, failure.cost, failure.outputs = usage, cost, outputs
                return failure
            validation = await run_aux_commands(
                ctx, cfg.get("validation_commands") or [], label="validation", cwd=cwd
            )
            if validation is not None:
                validation.usage, validation.cost = usage, cost
                return validation
            return AdapterResult.success(
                exit_code=proc.exit_code, usage=usage, cost=cost, outputs=outputs
            )
        finally:
            if minted_token:
                revoke_attempt_token(ctx.data_dir, ctx.attempt.id)
                remove_launch_config(ctx.data_dir, ctx.attempt.id)
