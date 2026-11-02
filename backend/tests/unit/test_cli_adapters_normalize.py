"""Normalization of recorded Claude Code / Codex CLI / Generic Command output (no real CLIs)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from agenttape.adapters.builtin.claude_code import ClaudeCodeAdapter, ClaudeStreamNormalizer
from agenttape.adapters.builtin.codex_cli import CodexCliAdapter, CodexEventNormalizer
from agenttape.adapters.builtin.generic_command import GenericCommandAdapter
from agenttape.adapters.policy import ProfilePolicy
from agenttape.domain.events import EventDraft
from agenttape.mcp.launch import FINDINGS_PROMPT_CONTRACT, RECORD_FINDING_TOOL
from agenttape.security import executables

FIXTURES = Path(__file__).parent.parent / "fixtures" / "adapters"


def records(name: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines() if line]


def all_drafts(normalizer: Any, name: str) -> list[EventDraft]:
    out: list[EventDraft] = []
    for raw in records(name):
        out.extend(normalizer.feed(raw))
    return out


# --------------------------------------------------------------------------- claude code


def test_claude_stream_maps_to_envelope() -> None:
    n = ClaudeStreamNormalizer()
    drafts = all_drafts(n, "claude_code_success.jsonl")
    types = [d.type.value for d in drafts]  # type: ignore[union-attr]
    assert types == [
        "lifecycle",
        "agent_message",
        "tool_call",
        "tool_result",
        "tool_call",
        "tool_result",
        "warning",
        "agent_message",
        "usage",
    ]
    assert drafts[0].payload["model"] == "claude-sonnet-5"
    assert drafts[0].payload["session_id"] == "8f0c2a4e-1b7d-4c55-9a43-2f1e6d0b9c11"
    # tool results link to their calls
    assert drafts[3].parent_event_id == drafts[2].id
    assert drafts[5].parent_event_id == drafts[4].id
    assert drafts[2].summary == "Read(/work/demo-repo/src/greeter.py)"
    assert drafts[5].payload["is_error"] is True
    assert drafts[5].payload["output"] == "1 failed, 3 passed in 0.12s"
    assert all(d.source == "claude-code" for d in drafts)


def test_claude_hidden_reasoning_is_never_recorded() -> None:
    drafts = all_drafts(ClaudeStreamNormalizer(), "claude_code_success.jsonl")
    blob = json.dumps([d.payload for d in drafts] + [d.summary for d in drafts])
    assert "HIDDEN-REASONING" not in blob
    assert not any(d.type.value == "reasoning_summary" for d in drafts)  # type: ignore[union-attr]


def test_claude_usage_and_cost_exactly_as_reported() -> None:
    n = ClaudeStreamNormalizer()
    drafts = all_drafts(n, "claude_code_success.jsonl")
    assert n.usage() == {
        "input_tokens": 5700,
        "output_tokens": 210,
        "cache_creation_input_tokens": 2048,
        "cache_read_input_tokens": 8192,
    }
    cost = n.cost()
    assert cost is not None and cost["total_usd"] == pytest.approx(0.04213)
    assert "estimate" in cost["source"]
    usage_event = drafts[-1]
    assert usage_event.usage == n.usage() and usage_event.cost == cost
    assert "simulated" not in (usage_event.usage or {})


def test_claude_auth_error_is_detected() -> None:
    n = ClaudeStreamNormalizer()
    all_drafts(n, "claude_code_auth_error.jsonl")
    assert n.auth_error == "authentication_failed"
    assert n.result is not None and n.result["is_error"] is True


def test_claude_argv_uses_structured_non_interactive_mode() -> None:
    argv = ClaudeCodeAdapter().build_argv(
        "/usr/bin/claude",
        {
            "model": "sonnet",
            "permission_mode": "acceptEdits",
            "max_turns": 7,
            "allowed_tools": ["Read", "Bash(git diff *)"],
            "denied_tools": ["WebFetch"],
            "budget": {"max_cost_usd": 0.5},
        },
    )
    assert argv[:5] == ["/usr/bin/claude", "-p", "--output-format", "stream-json", "--verbose"]
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert argv[argv.index("--max-turns") + 1] == "7"
    assert argv[argv.index("--allowedTools") + 1] == "Read,Bash(git diff *)"
    assert argv[argv.index("--disallowedTools") + 1] == "WebFetch"
    assert argv[argv.index("--max-budget-usd") + 1] == "0.5"


def test_claude_argv_wires_the_agenttape_mcp_server(tmp_path: Path) -> None:
    mcp_config = tmp_path / "attempt.mcp.json"
    argv = ClaudeCodeAdapter().build_argv(
        "/usr/bin/claude",
        {"bare": True, "model": "sonnet"},
        mcp_config=mcp_config,
    )
    bare_i = argv.index("--bare")
    mcp_i = argv.index("--mcp-config")
    assert argv[mcp_i - 1 : mcp_i + 5] == [
        "--bare",
        "--mcp-config",
        str(mcp_config),
        "--strict-mcp-config",
        "--append-system-prompt",
        FINDINGS_PROMPT_CONTRACT,
    ]
    assert mcp_i == bare_i + 1
    contract = argv[argv.index("--append-system-prompt") + 1]
    assert "\n" not in contract
    assert contract == FINDINGS_PROMPT_CONTRACT
    assert argv[argv.index("--allowedTools") + 1] == RECORD_FINDING_TOOL

    argv_no_profile_tools = ClaudeCodeAdapter().build_argv(
        "/usr/bin/claude", {}, mcp_config=mcp_config
    )
    assert argv_no_profile_tools[argv_no_profile_tools.index("--allowedTools") + 1] == (
        RECORD_FINDING_TOOL
    )

    argv_with_profile_tools = ClaudeCodeAdapter().build_argv(
        "/usr/bin/claude", {"allowed_tools": ["Read"]}, mcp_config=mcp_config
    )
    assert (
        argv_with_profile_tools[argv_with_profile_tools.index("--allowedTools") + 1]
        == f"Read,{RECORD_FINDING_TOOL}"
    )

    argv_without_mcp = ClaudeCodeAdapter().build_argv("/usr/bin/claude", {"model": "sonnet"})
    assert "--mcp-config" not in argv_without_mcp
    assert "--strict-mcp-config" not in argv_without_mcp
    assert "--append-system-prompt" not in argv_without_mcp
    assert "--allowedTools" not in argv_without_mcp


def test_claude_argv_respects_a_denied_record_finding_tool(tmp_path: Path) -> None:
    mcp_config = tmp_path / "attempt.mcp.json"
    for denial in (RECORD_FINDING_TOOL, "mcp__agenttape", "mcp__agenttape__*"):
        argv = ClaudeCodeAdapter().build_argv(
            "/usr/bin/claude",
            {"denied_tools": [denial]},
            mcp_config=mcp_config,
        )
        assert "--allowedTools" not in argv
        assert argv[argv.index("--disallowedTools") + 1] == denial


def test_claude_validation_rejects_unsafe_config() -> None:
    issues = ClaudeCodeAdapter().validate_profile(
        {
            "permission_mode": "yolo",
            "extra_args": ["--dangerously-skip-permissions"],
            "max_turns": 0,
            "args": "rm -rf /",
        }
    )
    fields = {i.field for i in issues if i.severity == "error"}
    assert {"permission_mode", "extra_args", "max_turns", "args"} <= fields
    warn = ClaudeCodeAdapter().validate_profile({"permission_mode": "bypassPermissions"})
    assert [i.severity for i in warn] == ["warning"]


# --------------------------------------------------------------------------- codex


def test_codex_jsonl_maps_to_envelope() -> None:
    n = CodexEventNormalizer()
    drafts = all_drafts(n, "codex_exec_success.jsonl")
    types = [d.type.value for d in drafts]  # type: ignore[union-attr]
    assert types == [
        "lifecycle",
        "reasoning_summary",
        "tool_call",
        "tool_result",
        "agent_message",
        "file_write",
        "file_write",
        "tool_call",
        "tool_result",
        "tool_call",
        "tool_result",
        "agent_message",
        "usage",
    ]
    call, result = drafts[2], drafts[3]
    assert call.payload["tool"] == "shell" and result.parent_event_id == call.id
    assert result.payload["exit_code"] == 0 and result.payload["is_error"] is False
    failed = drafts[10]
    assert failed.payload["is_error"] is True and failed.payload["exit_code"] == 1
    assert drafts[5].payload == {
        "path": "src/greeter.py",
        "operation": "update",
        "status": "completed",
    }
    assert drafts[7].payload["tool"] == "docs.search"
    assert n.usage == {
        "input_tokens": 24763,
        "cached_input_tokens": 24448,
        "output_tokens": 122,
        "reasoning_output_tokens": 0,
    }
    assert n.last_message == "Validation added and tests updated."
    assert n.failed is None


def test_codex_failure_is_recorded() -> None:
    n = CodexEventNormalizer()
    drafts = all_drafts(n, "codex_exec_failed.jsonl")
    assert [d.type.value for d in drafts] == ["lifecycle", "error", "error"]  # type: ignore[union-attr]
    assert n.failed == "stream disconnected before completion"
    assert n.usage is None


def test_codex_argv_reads_prompt_from_stdin() -> None:
    argv = CodexCliAdapter().build_argv(
        "/bin/codex", {"model": "gpt-5", "sandbox": "read-only"}, "/work"
    )
    assert argv[:3] == ["/bin/codex", "exec", "--json"]
    assert argv[-1] == "-"
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert argv[argv.index("--cd") + 1] == "/work"


def test_codex_validation() -> None:
    issues = CodexCliAdapter().validate_profile(
        {"sandbox": "nope", "extra_args": ["--dangerously-bypass-approvals-and-sandbox"]}
    )
    assert {i.field for i in issues if i.severity == "error"} == {"sandbox", "extra_args"}


# --------------------------------------------------------------------------- generic command


def test_generic_jsonl_protocol() -> None:
    adapter = GenericCommandAdapter()
    [msg] = adapter.normalize(
        {"agenttape": 1, "type": "agent_message", "summary": "hi", "payload": {"text": "hi"}}
    )
    assert msg.type.value == "agent_message" and msg.summary == "hi"  # type: ignore[union-attr]
    [usage] = adapter.normalize(
        {"agenttape": 1, "type": "usage", "payload": {"input_tokens": 3, "note": "x"}}
    )
    assert usage.usage == {"input_tokens": 3}
    [bad] = adapter.normalize({"agenttape": 1, "type": "lifecycle"})
    assert bad.type.value == "warning"  # type: ignore[union-attr]


def test_generic_validation_requires_argv_arrays() -> None:
    adapter = GenericCommandAdapter()
    fields = {i.field for i in adapter.validate_profile({"args": "--flag value"})}
    assert {"executable", "args"} <= fields
    issues = adapter.validate_profile(
        {
            "executable": "/usr/bin/env",
            "args": ["a"],
            "workdir_mode": "subdirectory",
            "working_dir": "../escape",
            "env_allowlist": ["OK_NAME", "bad name"],
            "secret_env": {"TOKEN": "sk-live-value with spaces"},
            "prompt_mode": "shell",
        }
    )
    fields = {i.field for i in issues}
    assert {"working_dir", "env_allowlist[1]", "secret_env.TOKEN", "prompt_mode"} <= fields


# --------------------------------------------------------------------------- executable allowlist


def _exe(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def test_executable_allowlist(tmp_path: Path) -> None:
    data = tmp_path / "data"
    tool = _exe(tmp_path / "tool")
    link = tmp_path / "link"
    link.symlink_to(tool)
    check = executables.check_executable(str(tool), data)
    assert not check.ok and "allowlist" in (check.error or "")
    executables.save_entries(data, [str(link)])
    assert executables.stored_entries(data) == [str(tool)]  # canonicalized
    assert executables.check_executable(str(link), data).ok
    assert executables.check_executable(str(tool), data).allowed_by == "allowlist"
    missing = executables.check_executable(str(tmp_path / "nope"), data)
    assert missing.path is None and "not found" in (missing.error or "")
    plain = tmp_path / "plain"
    plain.write_text("x")
    assert "not executable" in (executables.check_executable(str(plain), data).error or "")
    assert not executables.check_executable("relative/tool", data).ok


def test_executable_env_allowlist_and_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    tool = _exe(tmp_path / "mytool")
    monkeypatch.setenv(executables.ENV_VAR, f"{tmp_path / 'other'},{tool}")
    assert executables.check_executable(str(tool), data).ok
    monkeypatch.delenv(executables.ENV_VAR)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    assert executables.check_executable("mytool", data, default_names=("mytool",)).ok
    assert not executables.check_executable("mytool", data).ok


def test_policy_validation_flags_unlisted_executable(tmp_path: Path) -> None:
    data = tmp_path / "data"
    tool = _exe(tmp_path / "tool")
    policy = ProfilePolicy(data_dir=data, secret_names=frozenset({"KNOWN"}))
    adapter = GenericCommandAdapter()
    cfg: dict[str, Any] = {
        "executable": str(tool),
        "secret_env": {"A": "KNOWN", "B": "UNKNOWN"},
        "validation_commands": [[str(tool), "--check"]],
    }
    issues = adapter.validate_with_policy(cfg, policy)
    assert {(i.field, i.severity) for i in issues} == {
        ("executable", "error"),
        ("validation_commands[0]", "error"),
        ("secret_env.B", "warning"),
    }
    executables.save_entries(data, [str(tool)])
    assert [i.severity for i in adapter.validate_with_policy(cfg, policy)] == ["warning"]
