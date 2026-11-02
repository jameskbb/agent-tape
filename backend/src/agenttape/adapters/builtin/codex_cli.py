"""Codex CLI adapter (``codex-cli``).

Runs ``codex exec --json`` (documented non-interactive mode; tested against codex-cli
0.156.1) with the rendered prompt on stdin (``-`` as the prompt argument) and maps each
JSONL event to the envelope. See docs/integrations/codex-cli.md.

Mapping summary:

* ``thread.started``                        -> ``lifecycle`` (thread id)
* ``turn.started``                          -> dropped (noise)
* ``item.*`` ``agent_message``              -> ``agent_message`` (on completion)
* ``item.*`` ``reasoning``                  -> ``reasoning_summary`` (the CLI's own summary text)
* ``item.started`` ``command_execution``    -> ``tool_call`` (tool ``shell``)
* ``item.completed`` ``command_execution``  -> ``tool_result`` (output, exit code)
* ``item.completed`` ``file_change``        -> ``file_write`` per changed path
* ``item.*`` ``mcp_tool_call``              -> ``tool_call`` / ``tool_result``
* ``item.completed`` ``web_search``         -> ``tool_call`` (tool ``web_search``)
* ``item.*`` ``todo_list``                  -> ``agent_message`` (plan update)
* ``item.completed`` ``error``              -> ``warning``
* ``turn.completed``                        -> ``usage`` (token counts as reported)
* ``turn.failed`` / ``error``               -> ``error``

Codex does not report cost, so cost stays unavailable.
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
from agenttape.adapters.builtin._process_cli import (
    COMMON_SCHEMA,
    ExecutionSetupError,
    child_env,
    default_executable_for,
    discover_cli,
    gate_commands,
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
from agenttape.domain.usage import merge_usage
from agenttape.infrastructure.db.base import new_id

ENV_OVERRIDE = "AGENTTAPE_CODEX_EXECUTABLE"
DEFAULT_NAME = "codex"
TESTED_VERSION = "0.156.1"
SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
FORBIDDEN_EXTRA = {
    "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-bypass-hook-trust",
    "--json",
    "--worktree",
    "-o",
    "--output-last-message",
}
USAGE_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")


class CodexEventNormalizer:
    """Stateful mapper for one ``codex exec --json`` stream."""

    def __init__(self, source: str = "codex-cli") -> None:
        self.source = source
        self.item_events: dict[str, str] = {}
        self.usage: dict[str, Any] | None = None
        self.thread_id: str | None = None
        self.last_message: str | None = None
        self.failed: str | None = None
        self.turns_completed = 0

    def _draft(
        self, etype: EventType, summary: str, payload: dict[str, Any], **kw: Any
    ) -> EventDraft:
        return EventDraft(etype, summary[:500], payload, source=self.source, **kw)

    def feed(self, raw: dict[str, Any]) -> list[EventDraft]:
        kind = str(raw.get("type", ""))
        if kind == "thread.started":
            self.thread_id = raw.get("thread_id")
            return [
                self._draft(
                    EventType.LIFECYCLE,
                    "Codex session started",
                    {"phase": "session_started", "thread_id": raw.get("thread_id")},
                )
            ]
        if kind == "turn.started":
            return []
        if kind == "turn.completed":
            self.turns_completed += 1
            raw_usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
            assert isinstance(raw_usage, dict)
            usage = {
                k: raw_usage[k]
                for k in USAGE_KEYS
                if isinstance(raw_usage.get(k), int | float)
                and not isinstance(raw_usage.get(k), bool)
            } or None
            self.usage = merge_usage(self.usage, usage)
            if usage is None:
                return []
            return [
                self._draft(
                    EventType.USAGE,
                    f"Codex reported {usage.get('input_tokens', '?')} in / "
                    f"{usage.get('output_tokens', '?')} out tokens",
                    dict(usage),
                    usage=usage,
                )
            ]
        if kind == "turn.failed":
            error = raw.get("error") if isinstance(raw.get("error"), dict) else {}
            assert isinstance(error, dict)
            self.failed = str(error.get("message") or "turn failed")
            return [
                self._draft(
                    EventType.ERROR,
                    f"Codex turn failed: {self.failed}",
                    {"error": self.failed},
                    severity=Severity.ERROR,
                )
            ]
        if kind == "error":
            message = str(raw.get("message") or "error")
            self.failed = self.failed or message
            return [
                self._draft(
                    EventType.ERROR,
                    f"Codex error: {message}",
                    {"error": message},
                    severity=Severity.ERROR,
                )
            ]
        if kind.startswith("item."):
            item = raw.get("item") if isinstance(raw.get("item"), dict) else None
            if item is None:
                return []
            return self._item(kind.split(".", 1)[1], item)
        return [
            self._draft(
                EventType.AGENT_MESSAGE,
                f"Codex record: {kind}",
                {"role": "system", "raw_type": kind},
                severity=Severity.DEBUG,
            )
        ]

    def _item(self, phase: str, item: dict[str, Any]) -> list[EventDraft]:
        itype = item.get("type")
        item_id = str(item.get("id", ""))
        done = phase == "completed"
        if itype == "agent_message":
            if not done:
                return []
            text = str(item.get("text", ""))
            self.last_message = text
            return [self._draft(EventType.AGENT_MESSAGE, text, {"role": "assistant", "text": text})]
        if itype == "reasoning":
            if not done:
                return []
            text = str(item.get("text", ""))
            return [
                self._draft(
                    EventType.REASONING_SUMMARY,
                    text,
                    {"text": text, "provided_by": "codex-cli reasoning item"},
                )
            ]
        if itype == "command_execution":
            return self._tool(
                phase,
                item_id,
                tool="shell",
                call_summary=f"shell({str(item.get('command', ''))[:160]})",
                call_input={"command": item.get("command")},
                output=str(item.get("aggregated_output") or ""),
                is_error=item.get("status") in ("failed", "declined")
                or (isinstance(item.get("exit_code"), int) and item.get("exit_code") != 0),
                extra={"exit_code": item.get("exit_code"), "status": item.get("status")},
            )
        if itype == "mcp_tool_call":
            name = f"{item.get('server', 'mcp')}.{item.get('tool', 'tool')}"
            result = item.get("result")
            error = item.get("error")
            output = json.dumps(result)[:16000] if result is not None else ""
            if error:
                output = str(error.get("message") if isinstance(error, dict) else error)
            return self._tool(
                phase,
                item_id,
                tool=name,
                call_summary=name,
                call_input={"arguments": item.get("arguments")},
                output=output,
                is_error=bool(error) or item.get("status") == "failed",
                extra={"status": item.get("status")},
            )
        if itype == "web_search":
            if not done:
                return []
            query = str(item.get("query", ""))
            return [
                self._draft(
                    EventType.TOOL_CALL,
                    f"web_search({query[:160]})",
                    {"tool": "web_search", "input": {"query": query}, "tool_use_id": item_id},
                )
            ]
        if itype == "file_change":
            if not done:
                return []
            drafts = []
            for change in item.get("changes") or []:
                if not isinstance(change, dict):
                    continue
                kind = str(change.get("kind", "update"))
                drafts.append(
                    self._draft(
                        EventType.FILE_WRITE,
                        f"{kind.capitalize()} {change.get('path')}",
                        {
                            "path": change.get("path"),
                            "operation": kind,
                            "status": item.get("status"),
                        },
                    )
                )
            return drafts
        if itype == "todo_list":
            items = item.get("items") if isinstance(item.get("items"), list) else []
            assert isinstance(items, list)
            lines = [
                f"[{'x' if i.get('completed') else ' '}] {i.get('text', '')}"
                for i in items
                if isinstance(i, dict)
            ]
            return [
                self._draft(
                    EventType.AGENT_MESSAGE,
                    f"Plan {'updated' if phase != 'started' else 'created'} ({len(lines)} items)",
                    {"role": "assistant", "text": "\n".join(lines), "kind": "plan"},
                )
            ]
        if itype == "error":
            message = str(item.get("message", "error"))
            return [
                self._draft(
                    EventType.WARNING,
                    f"Codex: {message}",
                    {"message": message},
                    severity=Severity.WARNING,
                )
            ]
        return [
            self._draft(
                EventType.AGENT_MESSAGE,
                f"Codex item: {itype}",
                {"role": "system", "item_type": itype, "phase": phase},
                severity=Severity.DEBUG,
            )
        ]

    def _tool(
        self,
        phase: str,
        item_id: str,
        *,
        tool: str,
        call_summary: str,
        call_input: dict[str, Any],
        output: str,
        is_error: bool,
        extra: dict[str, Any],
    ) -> list[EventDraft]:
        drafts: list[EventDraft] = []
        if phase in ("started", "completed") and item_id not in self.item_events:
            event_id = new_id()
            self.item_events[item_id] = event_id
            drafts.append(
                self._draft(
                    EventType.TOOL_CALL,
                    call_summary,
                    {"tool": tool, "input": call_input, "tool_use_id": item_id},
                    id=event_id,
                )
            )
        if phase == "completed":
            first = output.strip().splitlines()[0][:200] if output.strip() else "(no output)"
            drafts.append(
                self._draft(
                    EventType.TOOL_RESULT,
                    ("Tool error: " if is_error else "Tool result: ") + first,
                    {"tool_use_id": item_id, "output": output, "is_error": is_error, **extra},
                    severity=Severity.ERROR if is_error else Severity.INFO,
                    parent_event_id=self.item_events.get(item_id),
                )
            )
        return drafts


@register_adapter
class CodexCliAdapter(AgentAdapter):
    name = "codex-cli"
    display_name = "Codex CLI"
    description = (
        "OpenAI's Codex CLI in non-interactive mode (codex exec --json): messages, commands, "
        "file changes, MCP tool calls, and token usage. Codex does not report cost."
    )
    capabilities = frozenset(
        {"structured_events", "tool_calls", "usage", "model_selection", "sandbox"}
    )

    def discover(self) -> DiscoveryResult:
        status, version, exe, message, details = discover_cli(
            self.name,
            env_override=ENV_OVERRIDE,
            default_name=DEFAULT_NAME,
            setup_hint=f"Install the Codex CLI or set {ENV_OVERRIDE} to its path.",
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
        sandbox = config.get("sandbox")
        if sandbox is not None and sandbox not in SANDBOX_MODES:
            issues.append(ProfileIssue("sandbox", f"must be one of: {', '.join(SANDBOX_MODES)}"))
        if sandbox == "danger-full-access":
            issues.append(
                ProfileIssue(
                    "sandbox",
                    "danger-full-access lets model-generated commands touch the whole machine",
                    severity="warning",
                )
            )
        model = config.get("model")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            issues.append(ProfileIssue("model", "must be a model name"))
        issues.extend(validate_argv_list("extra_args", config.get("extra_args")))
        for arg in config.get("extra_args") or []:
            if isinstance(arg, str) and arg.split("=", 1)[0] in FORBIDDEN_EXTRA:
                issues.append(ProfileIssue("extra_args", f"{arg} is not allowed here"))
        for key in ("allowed_tools", "denied_tools"):
            if config.get(key):
                issues.append(
                    ProfileIssue(
                        key,
                        "Codex CLI has no per-tool allow/deny flag; use sandbox instead",
                        severity="warning",
                    )
                )
        if config.get("max_turns"):
            issues.append(
                ProfileIssue("max_turns", "Codex CLI has no max-turns flag", severity="warning")
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
                "model": {"type": "string"},
                "sandbox": {"type": "string", "enum": list(SANDBOX_MODES)},
                "skip_git_repo_check": {"type": "boolean", "default": False},
                "ephemeral": {"type": "boolean", "default": False},
                "extra_args": {"type": "array", "items": {"type": "string"}},
            },
        }

    def default_config(self) -> dict[str, Any]:
        return {"sandbox": "workspace-write"}

    def build_argv(self, exe: str, config: dict[str, Any], cwd: str | None) -> list[str]:
        argv = [exe, "exec", "--json", "--color", "never"]
        if config.get("model"):
            argv += ["--model", str(config["model"])]
        if config.get("sandbox"):
            argv += ["--sandbox", str(config["sandbox"])]
        if config.get("skip_git_repo_check"):
            argv.append("--skip-git-repo-check")
        if config.get("ephemeral"):
            argv.append("--ephemeral")
        if cwd:
            argv += ["--cd", cwd]
        argv += [str(a) for a in config.get("extra_args") or []]
        argv.append("-")  # read the prompt from stdin
        return argv

    def normalize(self, raw: dict[str, Any]) -> list[EventDraft]:
        return CodexEventNormalizer(self.name).feed(raw)

    async def execute(self, ctx: ExecutionContext) -> AdapterResult:
        cfg = ctx.profile
        try:
            default, trusted = default_executable_for(DEFAULT_NAME, ENV_OVERRIDE)
            exe = resolve_launch(ctx, default_executable=default, default_names=trusted)
            cwd = resolve_workdir(ctx)
        except ExecutionSetupError as exc:
            await ctx.events.event(EventType.ERROR, exc.message, {}, severity=Severity.ERROR)
            return exc.result()
        gated = await gate_commands(
            ctx, self.build_argv(str(exe), cfg, str(cwd) if cwd else None), cwd
        )
        if gated is not None:
            return gated
        preflight = await run_aux_commands(
            ctx, cfg.get("preflight_commands") or [], label="preflight", cwd=cwd
        )
        if preflight is not None:
            return preflight

        normalizer = CodexEventNormalizer(self.name)

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

        argv = self.build_argv(str(exe), cfg, str(cwd) if cwd else None)
        spec = CommandSpec(
            argv=argv,
            cwd=cwd,
            env=child_env(ctx),
            stdin=(ctx.resolved_prompt() or "").encode(),
            display=" ".join([exe.name, *argv[1:]])[:300],
        )
        proc = await run_process(spec, ctx, on_stdout=on_line)
        outputs = {"thread_id": normalizer.thread_id, "last_message": normalizer.last_message}
        failure = process_failure(proc, "Codex CLI")
        if failure is None and normalizer.failed:
            failure = AdapterResult.failure(
                FailureCategory.ADAPTER_ERROR,
                f"Codex reported a failure: {normalizer.failed}",
                exit_code=proc.exit_code,
            )
        if failure is not None:
            failure.usage, failure.outputs = normalizer.usage, outputs
            return failure
        validation = await run_aux_commands(
            ctx, cfg.get("validation_commands") or [], label="validation", cwd=cwd
        )
        if validation is not None:
            validation.usage = normalizer.usage
            return validation
        return AdapterResult.success(
            exit_code=proc.exit_code, usage=normalizer.usage, outputs=outputs
        )
