"""generic-command, claude-code, and codex-cli executed through the real run engine against
fake executables (small scripts that replay recorded JSONL). No real CLI is ever launched."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from agenttape.adapters.process import MAX_LINE_BYTES, REDACTION_OVERLAP_BYTES
from agenttape.application.context import AppContext
from agenttape.application.schemas.catalog import AgentProfileCreate
from agenttape.application.schemas.runs import RunCreate
from agenttape.application.services.profiles import AgentProfileService
from agenttape.application.services.runs import RunService
from agenttape.domain.errors import ValidationError
from agenttape.security import executables
from agenttape.worker.worker import Worker
from tests.conftest import drive, event_types, simple_prompt, start_run

FIXTURES = Path(__file__).parent.parent / "fixtures" / "adapters"


def make_exe(ctx: AppContext, name: str, body: str, *, allow: bool = True) -> Path:
    """Write a Python script with a real shebang, chmod +x, and optionally allowlist it."""
    bin_dir = ctx.settings.data_dir.parent / "bin"
    bin_dir.mkdir(exist_ok=True)
    path = bin_dir / name
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)
    if allow:
        current = executables.stored_entries(ctx.settings.data_dir)
        executables.save_entries(ctx.settings.data_dir, [*current, str(path)])
    return path


def replay_script(fixture: str, *, exit_code: int = 0) -> str:
    return f"""
import json, os, sys
record = {{"argv": sys.argv[1:], "stdin": sys.stdin.read(), "cwd": os.getcwd(),
          "env_keys": sorted(os.environ)}}
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "last-call.json"), "w") as fh:
    json.dump(record, fh)
for line in open({str(FIXTURES / fixture)!r}):
    sys.stdout.write(line)
    sys.stdout.flush()
sys.exit({exit_code})
"""


def last_call(ctx: AppContext) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (ctx.settings.data_dir.parent / "bin" / "last-call.json").read_text()
    )
    return data


def profile(ctx: AppContext, adapter: str, name: str = "p", **config: Any) -> str:
    return (
        AgentProfileService(ctx)
        .create(AgentProfileCreate(name=name, adapter_type=adapter, config=config))
        .id
    )


# --------------------------------------------------------------------------- generic-command


def test_generic_command_success_with_stdin_prompt_and_redacted_secret(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "gc-" + "s3cr3t-" + "value-9876"
    monkeypatch.setenv("GC_TEST_SECRET", secret)
    exe = make_exe(
        ctx,
        "echoer",
        "import os, sys\n"
        "prompt = sys.stdin.read()\n"
        "print('prompt:', prompt)\n"
        "print('token:', os.environ.get('TOOL_TOKEN'))\n"
        "print('run:', os.environ.get('AGENTTAPE_RUN_ID'))\n"
        "print('leak:', os.environ.get('HOST_ONLY', 'absent'))\n",
    )
    monkeypatch.setenv("HOST_ONLY", "should-not-pass")
    pid = profile(
        ctx,
        "generic-command",
        executable=str(exe),
        args=["--flag", "value with spaces"],
        secret_env={"TOOL_TOKEN": "GC_TEST_SECRET"},
    )
    run_id = start_run(ctx, pid)
    detail = drive(ctx, run_id)
    assert detail.run.status == "succeeded", detail.run.failure_message
    events = RunService(ctx).list_events(run_id, limit=500).items
    text = "\n".join(str(e.payload.get("text", "")) for e in events if e.type == "stdout")
    assert "prompt: Do the thing." in text
    assert f"run: {run_id}" in text
    assert "leak: absent" in text
    assert secret not in json.dumps([e.model_dump(mode="json") for e in events])
    assert "[REDACTED" in text
    command = next(e for e in events if e.type == "command")
    assert command.payload["argv"][1:] == ["--flag", "value with spaces"]
    assert "TOOL_TOKEN" in command.payload["env_keys"]
    log = ctx.settings.logs_dir / run_id / "attempt-1.stdout.log"
    assert secret not in log.read_text()


def test_generic_command_jsonl_parser_and_nonzero_exit(ctx: AppContext) -> None:
    exe = make_exe(
        ctx,
        "jsonl",
        "import json, sys\n"
        "print(json.dumps({'agenttape': 1, 'type': 'agent_message', 'summary': 'working',"
        " 'payload': {'role': 'assistant', 'text': 'working'}}))\n"
        "print(json.dumps({'agenttape': 1, 'type': 'usage', 'payload': {'input_tokens': 10,"
        " 'output_tokens': 4}}))\n"
        "print('plain line')\n"
        "sys.exit(3)\n",
    )
    pid = profile(ctx, "generic-command", executable=str(exe), parser="jsonl", prompt_mode="none")
    detail = drive(ctx, start_run(ctx, pid))
    assert detail.run.status == "failed"
    assert detail.run.failure_category == "adapter_error"
    assert detail.attempts[0].exit_code == 3
    types = event_types(ctx, detail.run.id)
    assert "agent_message" in types and "usage" in types and "stdout" in types
    assert detail.run.usage == {"input_tokens": 10, "output_tokens": 4}


def test_an_over_long_output_line_is_split_into_bounded_pieces(ctx: AppContext) -> None:
    """A plain-output adapter keeps the default line bound: a line over it is delivered in
    ``MAX_LINE_BYTES`` pieces flagged truncated, and the next line arrives whole."""
    size = MAX_LINE_BYTES * 3 + 123
    exe = make_exe(
        ctx,
        "longline",
        f"import sys\nsys.stdout.write('a' * {size} + '\\n')\nsys.stdout.write('tail line\\n')\n",
    )
    pid = profile(ctx, "generic-command", executable=str(exe), prompt_mode="none")
    detail = drive(ctx, start_run(ctx, pid))
    assert detail.run.status == "succeeded", detail.run.failure_message
    log = ctx.settings.logs_dir / detail.run.id / "attempt-1.stdout.log"
    pieces = log.read_text().splitlines()
    assert pieces[-1] == "tail line"
    assert "".join(pieces[:-1]) == "a" * size
    assert all(len(p) <= MAX_LINE_BYTES + REDACTION_OVERLAP_BYTES for p in pieces)
    assert len(pieces[0]) == MAX_LINE_BYTES
    events = RunService(ctx).list_events(detail.run.id, limit=500).items
    assert any(e.type == "stdout" and e.payload.get("truncated") for e in events)


def test_generic_command_preflight_and_validation_commands(ctx: AppContext) -> None:
    ok = make_exe(ctx, "ok", "import sys; sys.exit(0)\n")
    bad = make_exe(ctx, "bad", "import sys; print('checks failed'); sys.exit(1)\n")
    pid = profile(
        ctx,
        "generic-command",
        executable=str(ok),
        preflight_commands=[[str(ok)]],
        validation_commands=[[str(bad), "--strict"]],
    )
    detail = drive(ctx, start_run(ctx, pid))
    assert detail.run.status == "failed"
    assert detail.run.failure_category == "validation_failed"


def test_profile_save_requires_allowlisted_executable(ctx: AppContext) -> None:
    exe = make_exe(ctx, "unlisted", "print('hi')\n", allow=False)
    with pytest.raises(ValidationError) as err:
        profile(ctx, "generic-command", executable=str(exe))
    issues = err.value.details["issues"]
    assert issues[0]["field"] == "executable" and "allowlist" in issues[0]["message"]


def test_removing_from_allowlist_blocks_existing_profiles_at_execution(ctx: AppContext) -> None:
    exe = make_exe(ctx, "later-removed", "print('hi')\n")
    pid = profile(ctx, "generic-command", executable=str(exe))
    executables.save_entries(ctx.settings.data_dir, [])
    detail = drive(ctx, start_run(ctx, pid))
    assert detail.run.status == "failed"
    assert detail.run.failure_category == "policy_violation"
    assert "command" not in event_types(ctx, detail.run.id)


def test_generic_command_timeout_kills_the_process(ctx: AppContext) -> None:
    exe = make_exe(ctx, "sleeper", "import time\nprint('start', flush=True)\ntime.sleep(60)\n")
    pid = profile(ctx, "generic-command", executable=str(exe), timeout_seconds=1)
    started = time.monotonic()
    detail = drive(ctx, start_run(ctx, pid), timeout=30)
    assert detail.run.status == "timed_out"
    assert time.monotonic() - started < 20
    assert "timeout" in event_types(ctx, detail.run.id)


async def test_generic_command_cancellation(ctx: AppContext) -> None:
    exe = make_exe(ctx, "long", "import time\nprint('ready', flush=True)\ntime.sleep(60)\n")
    pid = profile(ctx, "generic-command", executable=str(exe), timeout_seconds=120)
    run_id = start_run(ctx, pid)
    stop = asyncio.Event()
    worker = Worker(ctx, concurrency=1, poll_interval=0.05)
    task = asyncio.create_task(worker.run(stop))
    try:
        deadline = time.monotonic() + 20
        while "stdout" not in await asyncio.to_thread(event_types, ctx, run_id):
            assert time.monotonic() < deadline
            await asyncio.sleep(0.05)
        await asyncio.to_thread(RunService(ctx).cancel_run, run_id, reason="test")
        while (await asyncio.to_thread(RunService(ctx).get_run, run_id)).status != "cancelled":
            assert time.monotonic() < deadline
            await asyncio.sleep(0.05)
    finally:
        stop.set()
        await task
    detail = RunService(ctx).get_detail(run_id)
    assert detail.attempts[0].status == "cancelled"


# --------------------------------------------------------------------------- claude-code


def test_claude_code_replay_through_the_run_engine(ctx: AppContext) -> None:
    exe = make_exe(ctx, "claude", replay_script("claude_code_success.jsonl"))
    pid = profile(
        ctx,
        "claude-code",
        executable=str(exe),
        model="sonnet",
        permission_mode="acceptEdits",
        max_turns=5,
        allowed_tools=["Read"],
    )
    tid = simple_prompt(ctx)
    run = RunService(ctx).create_run(
        RunCreate(agent_profile_id=pid, prompt_template_id=tid, inputs={"thing": "the tests"})
    )
    detail = drive(ctx, run.run.id)
    assert detail.run.status == "succeeded", detail.run.failure_message
    call = last_call(ctx)
    assert call["argv"][:4] == ["-p", "--output-format", "stream-json", "--verbose"]
    assert "--max-turns" in call["argv"] and "--allowedTools" in call["argv"]
    assert call["stdin"] == "Please fix the tests."
    types = event_types(ctx, run.run.id)
    for expected in ("tool_call", "tool_result", "agent_message", "usage", "warning"):
        assert expected in types
    assert "reasoning_summary" not in types
    assert detail.run.usage and detail.run.usage["input_tokens"] == 5700
    assert detail.run.cost and detail.run.cost["total_usd"] == pytest.approx(0.04213)
    events = RunService(ctx).list_events(run.run.id, limit=500).items
    assert "HIDDEN-REASONING" not in json.dumps([e.model_dump(mode="json") for e in events])


def test_claude_code_read_of_redactable_code_keeps_its_tool_result(ctx: AppContext) -> None:
    """Recorded shape: a Read of source with a credential-shaped assignment.

    Redacting the serialized stream line as flat text rewrote the JSON escape in
    ``PREFIX = \\"atm_\\"`` and broke the line, so the tool_result was stored as raw stdout
    and the read's content was lost; it also swallowed ``\\n14\\t`` escape runs, merging
    lines of the recorded output.
    """
    fixture = FIXTURES / "claude_code_read_redactable_code.jsonl"
    exe = make_exe(ctx, "claude", replay_script(fixture.name))
    pid = profile(ctx, "claude-code", executable=str(exe), allowed_tools=["Read"])
    detail = drive(ctx, start_run(ctx, pid))
    assert detail.run.status == "succeeded", detail.run.failure_message
    events = RunService(ctx).list_events(detail.run.id, limit=500).items
    expected = {
        c["tool_use_id"]: c["content"]
        for rec in map(json.loads, fixture.read_text().splitlines())
        if rec["type"] == "user"
        for c in rec["message"]["content"]
    }
    results = {
        e.payload["tool_use_id"]: e.payload["output"] for e in events if e.type == "tool_result"
    }
    assert set(results) == set(expected)
    for tool_use_id, output in results.items():
        assert output.count("\n") == expected[tool_use_id].count("\n")
    assert "ATTEMPT_TOKEN_PREFIX = [REDACTED:assignment]" in "".join(results.values())
    assert "14\t    updated: float" in "".join(results.values())
    assert not [e for e in events if e.type == "stdout"]
    log = ctx.settings.logs_dir / detail.run.id / "attempt-1.stdout.log"
    for line in log.read_text().splitlines():
        assert isinstance(json.loads(line), dict)


def test_claude_code_auth_failure_is_a_config_error(ctx: AppContext) -> None:
    exe = make_exe(ctx, "claude", replay_script("claude_code_auth_error.jsonl", exit_code=1))
    pid = profile(ctx, "claude-code", executable=str(exe))
    detail = drive(ctx, start_run(ctx, pid))
    assert detail.run.status == "failed"
    assert detail.run.failure_category == "config_error"
    assert "authenticate" in (detail.run.failure_message or "")


def test_claude_code_missing_cli_is_adapter_unavailable(ctx: AppContext) -> None:
    # The autouse fixture points AGENTTAPE_CLAUDE_EXECUTABLE at a path that does not exist.
    with pytest.raises(ValidationError):
        profile(ctx, "claude-code")
    info = next(a for a in AgentProfileService(ctx).adapters() if a.name == "claude-code")
    assert info.status == "unavailable"


# --------------------------------------------------------------------------- codex-cli


def test_codex_replay_through_the_run_engine(ctx: AppContext) -> None:
    exe = make_exe(ctx, "codex", replay_script("codex_exec_success.jsonl"))
    pid = profile(ctx, "codex-cli", executable=str(exe), sandbox="read-only", model="gpt-5")
    detail = drive(ctx, start_run(ctx, pid))
    assert detail.run.status == "succeeded", detail.run.failure_message
    call = last_call(ctx)
    assert call["argv"][:2] == ["exec", "--json"] and call["argv"][-1] == "-"
    assert call["stdin"] == "Do the thing."
    types = event_types(ctx, detail.run.id)
    assert types.count("file_write") == 2 and "reasoning_summary" in types
    assert detail.run.usage and detail.run.usage["output_tokens"] == 122
    assert detail.run.cost is None  # Codex does not report cost


def test_codex_turn_failure_fails_the_run(ctx: AppContext) -> None:
    exe = make_exe(ctx, "codex", replay_script("codex_exec_failed.jsonl"))
    pid = profile(ctx, "codex-cli", executable=str(exe))
    detail = drive(ctx, start_run(ctx, pid))
    assert detail.run.status == "failed"
    assert "stream disconnected" in (detail.run.failure_message or "")


def test_discovery_reports_versions_from_fake_clis(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = make_exe(ctx, "fake-claude-v", "print('9.8.7 (Claude Code)')\n", allow=False)
    monkeypatch.setenv("AGENTTAPE_CLAUDE_EXECUTABLE", str(exe))
    info = next(a for a in AgentProfileService(ctx).adapters() if a.name == "claude-code")
    assert info.status == "installed" and info.version == "9.8.7"
    broken = make_exe(ctx, "fake-codex-broken", "import sys; sys.exit(2)\n", allow=False)
    monkeypatch.setenv("AGENTTAPE_CODEX_EXECUTABLE", str(broken))
    info = next(a for a in AgentProfileService(ctx).adapters() if a.name == "codex-cli")
    assert info.status == "misconfigured"
