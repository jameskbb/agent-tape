"""``agenttape mcp register|unregister|status``: registers the
AgentTape MCP server for interactive Claude Code sessions with one command, without ever
putting a secret in ``claude mcp add``'s argv or running the real ``claude`` CLI.
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agenttape.cli.main import app
from agenttape.config import load_settings
from agenttape.security.capture import CaptureTokenStore, capture_api_base

runner = CliRunner()

#: A fake AWS-shaped secret the fake ``claude`` prints on a forced failure, assembled at
#: runtime so the secret scan does not flag the test (mirrors test_mcp.py's own convention).
LEAK = "AKIA" + "QQQQWWWWEEEERRRR"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A temp ``HOME``/``AGENTTAPE_DATA_DIR``, and a fake ``claude`` (records every argv
    list it is called with as one JSON line; exits with the code in ``exit_code_path``,
    default 0) set as ``AGENTTAPE_CLAUDE_EXECUTABLE`` - the real ``claude`` on this
    machine's PATH is never invoked.
    """
    home = tmp_path / "home"
    home.mkdir()
    data_dir = tmp_path / "data"
    calls_path = tmp_path / "claude-calls.jsonl"
    exit_code_path = tmp_path / "claude-exit-code"
    claude = tmp_path / "claude"
    claude.write_text(
        f"""#!{sys.executable}
import json, sys
from pathlib import Path

argv = sys.argv[1:]
with open({str(calls_path)!r}, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\\n")

code_path = Path({str(exit_code_path)!r})
code = int(code_path.read_text()) if code_path.exists() else 0
if code != 0:
    sys.stderr.write("claude mcp failed: secret={LEAK}\\n")
sys.exit(code)
"""
    )
    claude.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGENTTAPE_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AGENTTAPE_CLAUDE_EXECUTABLE", str(claude))
    monkeypatch.setenv("AGENTTAPE_DEMO_STEP_DELAY_MS", "0")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return {
        "home": home,
        "data_dir": data_dir,
        "calls": calls_path,
        "exit_code": exit_code_path,
        "claude": claude,
    }


def invoke(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, ["mcp", *args])
    return result.exit_code, result.output


def calls(env: dict[str, Path]) -> list[list[str]]:
    path = env["calls"]
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def token_path(env: dict[str, Path]) -> Path:
    return env["data_dir"] / "capture" / "mcp-client.token"


def test_register_runs_remove_then_add_with_the_expected_argv(env: dict[str, Path]) -> None:
    code, out = invoke("register")
    assert code == 0, out
    recorded = calls(env)
    assert len(recorded) == 2
    remove_argv, add_argv = recorded
    assert remove_argv == ["mcp", "remove", "--scope", "user", "agenttape"]
    settings = load_settings()
    api_base = capture_api_base(settings)
    assert add_argv == [
        "mcp",
        "add",
        "--scope",
        "user",
        "agenttape",
        "-e",
        f"AGENTTAPE_DATA_DIR={env['data_dir']}",
        "-e",
        f"AGENTTAPE_API_URL={api_base}",
        "--",
        sys.executable,
        "-m",
        "agenttape.cli.main",
        "mcp",
        "serve",
    ]


def test_no_secret_in_argv_and_token_file_is_owner_only(env: dict[str, Path]) -> None:
    code, _out = invoke("register")
    assert code == 0
    blob = json.dumps(calls(env))
    assert "atc_" not in blob

    path = token_path(env)
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    token = path.read_text().strip()
    assert token.startswith("atc_")
    assert CaptureTokenStore(env["data_dir"]).verify("mcp", token)


def test_second_register_is_a_noop_for_the_token_and_rotate_changes_it(
    env: dict[str, Path],
) -> None:
    invoke("register")
    first_token = token_path(env).read_text()

    code, _out = invoke("register")
    assert code == 0
    assert token_path(env).read_text() == first_token

    code, _out = invoke("register", "--rotate")
    assert code == 0
    rotated_token = token_path(env).read_text()
    assert rotated_token != first_token
    assert CaptureTokenStore(env["data_dir"]).verify("mcp", rotated_token.strip())


def test_failing_add_exits_1_prints_redacted_stderr_and_revokes_the_minted_token(
    env: dict[str, Path],
) -> None:
    env["exit_code"].write_text("1")
    code, out = invoke("register")
    assert code == 1
    assert LEAK not in out
    assert "[REDACTED:" in out
    assert not token_path(env).exists()
    store = CaptureTokenStore(env["data_dir"])
    assert store.read_client_token("mcp") is None


def test_unregister_runs_remove_and_revokes_the_token(env: dict[str, Path]) -> None:
    invoke("register")
    assert token_path(env).exists()

    code, out = invoke("unregister")
    assert code == 0, out
    recorded = calls(env)
    assert recorded[-1] == ["mcp", "remove", "--scope", "user", "agenttape"]
    assert not token_path(env).exists()
    assert CaptureTokenStore(env["data_dir"]).read_client_token("mcp") is None


def test_unregister_exits_0_when_claude_reports_the_server_was_not_registered(
    env: dict[str, Path],
) -> None:
    env["exit_code"].write_text("1")
    code, out = invoke("unregister")
    assert code == 0, out
    assert "claude mcp remove reported" in out
    assert LEAK not in out
    assert "Removed the AgentTape MCP server" not in out
    assert "No mcp token was present." in out
    assert "Revoked" not in out


def test_unregister_says_what_it_removed_and_revoked(env: dict[str, Path]) -> None:
    invoke("register")
    code, out = invoke("unregister")
    assert code == 0, out
    assert "Removed the AgentTape MCP server from Claude Code (user scope)." in out
    assert "Revoked the mcp token." in out
    assert "(if any)" not in out


def test_no_claude_executable_exits_1_and_mints_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home2"
    home.mkdir()
    data_dir = tmp_path / "data2"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGENTTAPE_DATA_DIR", str(data_dir))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    (tmp_path / "empty-bin").mkdir()

    code, out = invoke("register")
    assert code == 1
    assert "AGENTTAPE_CLAUDE_EXECUTABLE" in out
    assert CaptureTokenStore(data_dir).read_client_token("mcp") is None


def test_print_command_prints_and_runs_nothing(env: dict[str, Path]) -> None:
    code, out = invoke("register", "--print-command")
    assert code == 0, out
    assert "mcp add --scope user agenttape" in out
    assert "-e AGENTTAPE_DATA_DIR=" in out
    assert "-e AGENTTAPE_API_URL=" in out
    # Runs nothing and mints nothing: no `claude` call, no token in the store or on disk.
    assert calls(env) == []
    assert not token_path(env).exists()
    assert CaptureTokenStore(env["data_dir"]).read_client_token("mcp") is None
    assert not (env["data_dir"] / "capture" / "tokens.json").exists()


def test_status_json_reports_the_registration_and_the_token(env: dict[str, Path]) -> None:
    code, out = invoke("status", "--json")
    assert code == 0, out
    # The fake `claude mcp get` exits 0, so Claude Code reports the server as registered.
    assert json.loads(out) == {"registered": True, "token_valid": False, "warnings": []}
    assert calls(env)[-1] == ["mcp", "get", "agenttape"]

    invoke("register")
    code, out = invoke("status", "--json")
    assert code == 0, out
    assert json.loads(out) == {"registered": True, "token_valid": True, "warnings": []}


def test_status_reports_not_registered_when_claude_mcp_get_fails(env: dict[str, Path]) -> None:
    env["exit_code"].write_text("1")
    code, out = invoke("status", "--json")
    assert code == 0, out
    assert json.loads(out) == {"registered": False, "token_valid": False, "warnings": []}

    code, out = invoke("status")
    assert code == 0, out
    assert "Registered with Claude Code: no" in out
    assert "Token valid: False" in out
    assert LEAK not in out


def test_status_registration_is_unknown_without_claude(
    env: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENTTAPE_CLAUDE_EXECUTABLE")
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    (tmp_path / "empty-bin").mkdir()
    code, out = invoke("status", "--json")
    assert code == 0, out
    body = json.loads(out)
    assert body["registered"] is None
    assert body["token_valid"] is False
    assert body["warnings"] == [
        "Claude Code was not found, so the registration could not be checked."
    ]
