"""``agenttape hooks setup|remove|status``: writes the user's Claude
Code settings safely (idempotent, backed up, atomic) and ties the written token to the
hooks endpoint.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agenttape.bootstrap import build_context
from agenttape.capture.claude_settings import HOOK_URL_PATH, hook_entry
from agenttape.cli.main import app
from agenttape.security.capture import CaptureTokenStore
from tests.conftest import make_settings
from tests.integration.capture_helpers import loopback_client, post_hook

runner = CliRunner()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Isolates ``hooks setup`` from the real ``~/.claude`` and the real data dir: a
    temp ``HOME``, ``CLAUDE_CONFIG_DIR`` explicitly unset, a temp ``AGENTTAPE_DATA_DIR``.
    """
    home = tmp_path / "home"
    home.mkdir()
    data_dir = tmp_path / "data"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGENTTAPE_DATA_DIR", str(data_dir))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("AGENTTAPE_DEMO_STEP_DELAY_MS", "0")
    # Never the machine's own Claude Code: `hooks status` reports its version.
    monkeypatch.setenv("AGENTTAPE_CLAUDE_EXECUTABLE", str(tmp_path / "no-claude-here"))
    return {"home": home, "data_dir": data_dir}


def minted_names(data_dir: Path) -> set[str]:
    """Names in the capture token store's file (hashes only; no token is read)."""
    path = data_dir / "capture" / "tokens.json"
    if not path.exists():
        return set()
    return set(json.loads(path.read_text(encoding="utf-8"))["tokens"])


def invoke(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, ["hooks", *args])
    return result.exit_code, result.output


def _default_path(env: dict[str, Path]) -> Path:
    return env["home"] / ".claude" / "settings.json"


def _hook_entry(doc: dict[str, object], event: str) -> dict[str, object]:
    groups = doc["hooks"][event]  # type: ignore[index]
    return groups[0]["hooks"][0]  # type: ignore[index]


def _bearer(entry: dict[str, object]) -> str:
    auth = entry["headers"]["Authorization"]  # type: ignore[index]
    assert isinstance(auth, str) and auth.startswith("Bearer ")
    return auth.removeprefix("Bearer ")


def test_setup_writes_default_path_under_home(env: dict[str, Path]) -> None:
    code, out = invoke("setup")
    assert code == 0, out
    path = _default_path(env)
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    doc = json.loads(path.read_text())
    assert set(doc["hooks"].keys()) == {
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "SessionStart",
        "SessionEnd",
    }


def test_setup_respects_claude_config_dir(
    env: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "custom-claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    code, out = invoke("setup")
    assert code == 0, out
    assert (config_dir / "settings.json").exists()
    assert not _default_path(env).exists()


def test_settings_option_wins_over_home_and_config_dir(
    env: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "custom-claude"))
    explicit = tmp_path / "explicit-settings.json"
    code, out = invoke("setup", "--settings", str(explicit))
    assert code == 0, out
    assert explicit.exists()
    assert not (tmp_path / "custom-claude" / "settings.json").exists()
    assert not _default_path(env).exists()


def test_token_verifies_and_tokens_json_has_no_plaintext(env: dict[str, Path]) -> None:
    invoke("setup")
    doc = json.loads(_default_path(env).read_text())
    token = _bearer(_hook_entry(doc, "PreToolUse"))
    assert CaptureTokenStore(env["data_dir"]).verify("hooks", token)
    tokens_raw = (env["data_dir"] / "capture" / "tokens.json").read_text()
    assert token not in tokens_raw


def test_second_setup_is_a_noop_and_rotate_changes_token(env: dict[str, Path]) -> None:
    invoke("setup")
    path = _default_path(env)
    before = path.read_bytes()
    old_token = _bearer(_hook_entry(json.loads(before), "PreToolUse"))

    code, out = invoke("setup")
    assert code == 0
    assert "already installed" in out
    assert path.read_bytes() == before
    assert CaptureTokenStore(env["data_dir"]).verify("hooks", old_token)

    code, out = invoke("setup", "--rotate")
    assert code == 0, out
    new_token = _bearer(_hook_entry(json.loads(path.read_text()), "PreToolUse"))
    assert new_token != old_token
    store = CaptureTokenStore(env["data_dir"])
    assert store.verify("hooks", new_token)
    assert not store.verify("hooks", old_token)


def test_second_setup_tightens_an_already_installed_readable_settings_file(
    env: dict[str, Path],
) -> None:
    """The "already installed, token still valid" no-op path must still tighten
    a settings.json that was made group/world-readable after the first setup - the file
    already carries a live bearer token either way, so a second `setup` call closes the
    same gap the first one did."""
    invoke("setup")
    path = _default_path(env)
    os.chmod(path, 0o644)

    code, out = invoke("setup")
    assert code == 0, out
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "owner-only (0600)" in out


def test_setup_preserves_existing_content_and_writes_a_backup(env: dict[str, Path]) -> None:
    path = _default_path(env)
    path.parent.mkdir(parents=True)
    original = {
        "permissions": {"allow": ["Bash"]},
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}
            ]
        },
    }
    path.write_text(json.dumps(original))
    os.chmod(path, 0o644)

    code, out = invoke("setup")
    assert code == 0, out
    assert stat.S_IMODE(path.stat().st_mode) == 0o600  # tightened: the file now holds a token
    doc = json.loads(path.read_text())
    assert doc["permissions"] == original["permissions"]
    assert {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "echo hi"}],
    } in doc["hooks"]["PreToolUse"]

    backup = path.with_name(path.name + ".agenttape.bak")
    assert json.loads(backup.read_text()) == original
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_setup_makes_a_world_readable_settings_file_owner_only(env: dict[str, Path]) -> None:
    """A pre-existing settings.json is typically 0644; once setup writes a live
    bearer token into it, the file must be owner-only, and the command must say so."""
    path = _default_path(env)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"env": {"FOO": "bar"}}))
    os.chmod(path, 0o644)

    code, out = invoke("setup")
    assert code == 0, out
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "owner-only (0600)" in out

    backup = path.with_name(path.name + ".agenttape.bak")
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_remove_does_not_tighten_a_broader_mode(env: dict[str, Path]) -> None:
    """Removing the token needs no tightening: a mode the user set after setup is left
    alone by ``remove``."""
    invoke("setup")
    path = _default_path(env)
    os.chmod(path, 0o644)

    code, out = invoke("remove")
    assert code == 0, out
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_remove_restores_the_original_and_revokes_the_token(env: dict[str, Path]) -> None:
    path = _default_path(env)
    path.parent.mkdir(parents=True)
    original = {"env": {"FOO": "bar"}}
    path.write_text(json.dumps(original))

    invoke("setup")
    installed = json.loads(path.read_text())
    token = _bearer(_hook_entry(installed, "PreToolUse"))

    code, out = invoke("remove")
    assert code == 0, out
    assert "Removed 5" in out
    assert json.loads(path.read_text()) == original
    assert not CaptureTokenStore(env["data_dir"]).verify("hooks", token)


def test_remove_on_a_file_without_hooks_keeps_the_relay_token_copy_and_says_so(
    env: dict[str, Path], tmp_path: Path
) -> None:
    """Another settings file may still hold the relay that reads the copy, and which one is
    not knowable, so `remove` on a file with no AgentTape hooks keeps it and says how to
    retire it."""
    invoke("setup")
    copy_path = env["data_dir"] / "capture" / "hooks-client.token"
    before = copy_path.read_text()
    other = tmp_path / "project" / "settings.json"
    other.parent.mkdir()
    other.write_text("{}\n")
    code, out = invoke("remove", "--settings", str(other))
    assert code == 0, out
    assert "No AgentTape hooks were installed" in out
    assert "kept" in out and "setup --rotate" in out
    assert copy_path.read_text() == before
    assert other.read_text() == "{}\n"
    payload = json.loads(invoke("status", "--json")[1])
    assert payload["relay"]["token_copy_valid"] is True


def test_remove_with_nothing_installed_is_a_noop(env: dict[str, Path]) -> None:
    code, out = invoke("remove")
    assert code == 0, out
    assert "No AgentTape hooks" in out
    assert not _default_path(env).exists()
    assert not env["data_dir"].exists()


def test_invalid_json_refuses_without_writing_or_minting(env: dict[str, Path]) -> None:
    path = _default_path(env)
    path.parent.mkdir(parents=True)
    path.write_text("{not json")

    code, out = invoke("setup")
    assert code == 1, out
    assert path.read_text() == "{not json"
    assert "hooks" not in minted_names(env["data_dir"])


def test_dry_run_masks_the_token_and_writes_nothing(env: dict[str, Path]) -> None:
    code, out = invoke("setup", "--dry-run")
    assert code == 0, out
    assert "atc_…" in out
    assert "Bearer atc_…" in out
    preview = json.loads(out)
    assert _hook_entry(preview, "SessionStart")["type"] == "command"
    assert not _default_path(env).exists()
    assert "hooks" not in minted_names(env["data_dir"])
    assert not (env["data_dir"] / "capture" / "hooks-client.token").exists()


def _relay_argv(entry: dict[str, object]) -> list[str]:
    args = entry["args"]
    assert isinstance(args, list)
    return [str(entry["command"]), *args]


def test_setup_installs_session_start_as_a_command_hook_relay(env: dict[str, Path]) -> None:
    """Claude Code does not run ``http`` hooks for SessionStart, so setup installs that one
    event as an exec-form ``command`` hook running ``agenttape hooks relay``: the absolute
    interpreter, a fixed argv, and no token in settings.json for it."""
    code, out = invoke("setup")
    assert code == 0, out
    assert "SessionStart" in out and "command hook" in out
    doc = json.loads(_default_path(env).read_text())
    start = _hook_entry(doc, "SessionStart")
    assert start == {
        "type": "command",
        "command": sys.executable,
        "args": [
            "-I",
            "-m",
            "agenttape.cli.hook_relay",
            "--data-dir",
            str(env["data_dir"]),
            "--url",
            f"http://127.0.0.1:8787{HOOK_URL_PATH}",
        ],
        "timeout": 10,
    }
    assert Path(sys.executable).is_absolute()
    assert "Authorization" not in json.dumps(start)
    for event in ("PreToolUse", "PostToolUse", "PostToolUseFailure", "SessionEnd"):
        assert _hook_entry(doc, event)["type"] == "http"


def test_setup_stores_the_relay_token_copy_owner_only(env: dict[str, Path]) -> None:
    invoke("setup")
    token = _bearer(_hook_entry(json.loads(_default_path(env).read_text()), "PreToolUse"))
    copy_path = env["data_dir"] / "capture" / "hooks-client.token"
    assert copy_path.read_text() == token
    assert stat.S_IMODE(copy_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(copy_path.parent.stat().st_mode) == 0o700
    assert CaptureTokenStore(env["data_dir"]).read_client_token("hooks") == token

    invoke("setup", "--rotate")
    rotated = _bearer(_hook_entry(json.loads(_default_path(env).read_text()), "PreToolUse"))
    assert rotated != token
    assert copy_path.read_text() == rotated


def test_setup_restores_a_missing_relay_token_copy_on_a_noop_rerun(env: dict[str, Path]) -> None:
    invoke("setup")
    path = _default_path(env)
    before = path.read_bytes()
    copy_path = env["data_dir"] / "capture" / "hooks-client.token"
    copy_path.unlink()

    code, out = invoke("setup")
    assert code == 0, out
    assert "already installed" in out
    assert path.read_bytes() == before
    token = _bearer(_hook_entry(json.loads(before), "PreToolUse"))
    assert copy_path.read_text() == token


def test_setup_upgrades_an_http_only_install_keeping_its_token(env: dict[str, Path]) -> None:
    """An install from before the relay has an ``http`` SessionStart hook Claude Code never
    runs and no token copy for a relay. Re-running setup swaps that one hook for the relay,
    keeps the still-valid token, and writes the copy the relay reads."""
    store = CaptureTokenStore(env["data_dir"])
    token = store.mint("hooks")  # the old setup kept no client copy
    url = f"http://127.0.0.1:8787{HOOK_URL_PATH}"
    entry = hook_entry(url, token)
    legacy = {
        "env": {"FOO": "bar"},
        "hooks": {
            **{e: [{"matcher": "*", "hooks": [entry]}] for e in ("PreToolUse", "PostToolUse")},
            "PostToolUseFailure": [{"matcher": "*", "hooks": [entry]}],
            "SessionStart": [{"hooks": [entry]}],
            "SessionEnd": [{"hooks": [entry]}],
        },
    }
    path = _default_path(env)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(legacy))
    os.chmod(path, 0o600)

    code, out = invoke("status", "--json")
    assert code == 0, out
    before = json.loads(out)
    assert before["http_events_claude_code_skips"] == ["SessionStart"]
    assert any("SessionStart" in w and "hooks setup" in w for w in before["warnings"])

    code, out = invoke("setup")
    assert code == 0, out
    assert "already installed" not in out
    doc = json.loads(path.read_text())
    assert doc["env"] == {"FOO": "bar"}
    assert _hook_entry(doc, "SessionStart")["type"] == "command"
    assert len(doc["hooks"]["SessionStart"]) == 1
    for event in ("PreToolUse", "PostToolUse", "PostToolUseFailure", "SessionEnd"):
        assert _bearer(_hook_entry(doc, event)) == token
    assert store.verify("hooks", token)
    assert store.read_client_token("hooks") == token

    code, out = invoke("status", "--json")
    after = json.loads(out)
    assert after["http_events_claude_code_skips"] == []
    assert after["relay"]["token_copy_valid"] is True
    assert after["warnings"] == []

    code, out = invoke("setup")
    assert "already installed" in out


def test_status_warns_when_the_relay_interpreter_is_gone(env: dict[str, Path]) -> None:
    invoke("setup")
    path = _default_path(env)
    doc = json.loads(path.read_text())
    doc["hooks"]["SessionStart"][0]["hooks"][0]["command"] = "/no/such/venv/bin/python"
    path.write_text(json.dumps(doc))
    _code, out = invoke("status", "--json")
    payload = json.loads(out)
    assert payload["relay"]["interpreter_exists"] is False
    assert any("/no/such/venv/bin/python" in w for w in payload["warnings"])


def test_status_flags_a_relay_written_by_an_older_setup_and_setup_replaces_it(
    env: dict[str, Path],
) -> None:
    """The first relay ran ``python -m`` without ``-I``, which puts the session's working
    directory first on ``sys.path``."""
    invoke("setup")
    path = _default_path(env)
    doc = json.loads(path.read_text())
    entry = doc["hooks"]["SessionStart"][0]["hooks"][0]
    assert entry["args"][0] == "-I"
    entry["args"] = [
        "-m",
        "agenttape.cli.main",
        "--data-dir",
        str(env["data_dir"]),
        "hooks",
        "relay",
        "--url",
        f"http://127.0.0.1:8787{HOOK_URL_PATH}",
    ]
    path.write_text(json.dumps(doc))
    os.chmod(path, 0o600)

    _code, out = invoke("status", "--json")
    payload = json.loads(out)
    assert any(
        "older AgentTape" in w and "working directory" in w and "hooks setup" in w
        for w in payload["warnings"]
    )

    code, out = invoke("setup")
    assert code == 0, out
    assert "already installed" not in out
    fixed = json.loads(path.read_text())
    assert fixed["hooks"]["SessionStart"] == [{"hooks": [_hook_entry(fixed, "SessionStart")]}]
    assert _hook_entry(fixed, "SessionStart")["args"][0] == "-I"  # type: ignore[index]
    _code, out = invoke("status", "--json")
    assert json.loads(out)["warnings"] == []


#: A user's own SessionStart command hooks: exec form and shell form.
USER_START_HOOKS = [
    {"type": "command", "command": "/usr/local/bin/my-start", "args": ["--quiet"]},
    {"type": "command", "command": "echo started >> ~/.cache/starts.log"},
]


def test_a_users_own_session_start_command_hooks_survive_setup_rotate_and_remove(
    env: dict[str, Path],
) -> None:
    path = _default_path(env)
    path.parent.mkdir(parents=True)
    original = {
        "hooks": {
            "SessionStart": [
                {"hooks": [USER_START_HOOKS[0]]},
                {"matcher": "startup", "hooks": [USER_START_HOOKS[1]]},
            ]
        }
    }
    path.write_text(json.dumps(original))
    os.chmod(path, 0o600)

    def user_hooks() -> list[object]:
        doc = json.loads(path.read_text())
        return [
            h
            for group in doc["hooks"]["SessionStart"]
            for h in group["hooks"]
            if h in USER_START_HOOKS
        ]

    for args in (("setup",), ("setup",), ("setup", "--rotate")):
        code, out = invoke(*args)
        assert code == 0, out
        assert user_hooks() == USER_START_HOOKS
        start = json.loads(path.read_text())["hooks"]["SessionStart"]
        assert len(start) == 3  # the user's two groups, and AgentTape's one
    code, out = invoke("remove")
    assert code == 0, out
    assert "Removed 5" in out
    assert json.loads(path.read_text()) == original


def test_status_warns_about_an_installed_interpreter_path_a_shell_would_split(
    env: dict[str, Path],
) -> None:
    invoke("setup")
    path = _default_path(env)
    doc = json.loads(path.read_text())
    doc["hooks"]["SessionStart"][0]["hooks"][0]["command"] = "/opt/Jane Doe/.venv/bin/python"
    path.write_text(json.dumps(doc))
    os.chmod(path, 0o600)
    payload = json.loads(invoke("status", "--json")[1])
    assert any("Jane Doe" in w and "treats specially" in w for w in payload["warnings"])


def _python_without_agenttape(tmp_path: Path) -> Path:
    """An interpreter that starts but cannot import AgentTape, as a system Python (or one
    that only had AgentTape through PYTHONPATH or the user site, which -I hides) fails."""
    exe = tmp_path / "bin" / "python3"
    exe.parent.mkdir(exist_ok=True)
    exe.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "sys.stderr.write(\"No module named 'agenttape'\\n\")\n"
        "sys.exit(1)\n"
    )
    exe.chmod(0o700)
    return exe


def test_setup_refuses_an_interpreter_that_cannot_run_the_relay(
    env: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checking that the interpreter file exists is not enough: setup runs the relay's own
    probe with it and refuses unless it answers."""
    monkeypatch.setattr(sys, "executable", str(_python_without_agenttape(tmp_path)))
    code, out = invoke("setup")
    assert code == 1
    assert "cannot run AgentTape's SessionStart relay" in out
    assert "No module named 'agenttape'" in out
    assert "was not changed" in out
    assert not _default_path(env).exists()
    assert "hooks" not in minted_names(env["data_dir"])


def test_setup_refuses_a_base_python_that_does_not_have_agenttape(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    if sys.prefix == sys.base_prefix:
        pytest.skip("the tests are not running in a virtualenv")
    base = str(Path(sys.base_prefix) / "bin" / Path(sys.executable).name)
    if not Path(base).is_file():
        pytest.skip(f"no base interpreter at {base}")
    monkeypatch.setattr(sys, "executable", base)
    code, out = invoke("setup")
    assert code == 1
    assert "cannot run AgentTape's SessionStart relay" in out
    assert not _default_path(env).exists()


def test_status_never_runs_an_interpreter_the_settings_file_names(
    env: dict[str, Path], tmp_path: Path
) -> None:
    """`hooks status --settings some/repo/.claude/settings.json` is a read-only look: it
    must not execute a binary that file names."""
    invoke("setup")
    marker = tmp_path / "ran-by-status"
    script = tmp_path / "bin" / "not-python"
    script.parent.mkdir()
    script.write_text(f'#!/bin/sh\necho "$@" > {marker}\n')
    script.chmod(0o700)
    path = _default_path(env)
    doc = json.loads(path.read_text())
    doc["hooks"]["SessionStart"][0]["hooks"][0]["command"] = str(script)
    path.write_text(json.dumps(doc))
    os.chmod(path, 0o600)
    payload = json.loads(invoke("status", "--json")[1])
    assert not marker.exists()
    assert payload["relay"]["interpreter_exists"] is True
    assert payload["relay"]["interpreter_runs_relay"] is None
    assert any(
        str(script) in w and "not the Python running this command" in w for w in payload["warnings"]
    )


def test_status_warns_when_the_relay_interpreter_cannot_run_the_relay(
    env: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Status probes only AgentTape's own interpreter: here the Python running it cannot
    import AgentTape on its own, and the hook names that same Python."""
    invoke("setup")
    path = _default_path(env)
    doc = json.loads(path.read_text())
    broken = str(_python_without_agenttape(tmp_path))
    monkeypatch.setattr(sys, "executable", broken)
    doc["hooks"]["SessionStart"][0]["hooks"][0]["command"] = broken
    path.write_text(json.dumps(doc))
    os.chmod(path, 0o600)
    payload = json.loads(invoke("status", "--json")[1])
    assert payload["relay"]["interpreter_exists"] is True
    assert payload["relay"]["interpreter_runs_relay"] is False
    assert any(broken in w and "cannot run" in w for w in payload["warnings"])


@pytest.mark.parametrize("home", ["jos\u00e9", "Zo\u00eb", "Jane Doe"])
def test_setup_accepts_a_data_dir_under_any_home_directory(
    env: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home: str
) -> None:
    """The data dir only ever appears in ``args``; no shell sees it."""
    data_dir = tmp_path / home / ".agenttape"
    monkeypatch.setenv("AGENTTAPE_DATA_DIR", str(data_dir))
    code, out = invoke("setup")
    assert code == 0, out
    start = _hook_entry(json.loads(_default_path(env).read_text()), "SessionStart")
    assert str(data_dir) in start["args"]  # type: ignore[operator]
    payload = json.loads(invoke("status", "--json")[1])
    assert payload["session_start_captured"] is True
    assert payload["warnings"] == []


def _venv_at(root: Path) -> Path:
    """A copy of this test run's virtualenv layout at ``root`` (symlinks), so an interpreter
    at a path of our choosing can still import AgentTape."""
    if sys.prefix == sys.base_prefix:
        pytest.skip("the tests are not running in a virtualenv")
    real = Path(sys.prefix)
    (root / "bin").mkdir(parents=True)
    (root / "pyvenv.cfg").write_text((real / "pyvenv.cfg").read_text())
    (root / "lib").symlink_to(real / "lib")
    exe = root / "bin" / Path(sys.executable).name
    exe.symlink_to(sys.executable)
    return exe


def test_setup_installs_the_relay_with_a_non_ascii_interpreter_path(
    env: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-ASCII letters are not special to bash or PowerShell."""
    exe = _venv_at(tmp_path / "jos\u00e9" / "venv")
    monkeypatch.setattr(sys, "executable", str(exe))
    code, out = invoke("setup")
    assert code == 0, out
    start = _hook_entry(json.loads(_default_path(env).read_text()), "SessionStart")
    assert start["command"] == str(exe)
    payload = json.loads(invoke("status", "--json")[1])
    assert payload["relay"]["interpreter_runs_relay"] is True
    assert payload["session_start_captured"] is True


def test_setup_with_an_interpreter_a_shell_would_split_installs_the_http_hooks_and_warns(
    env: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A space in the interpreter path costs SessionStart only, not the tool hooks: setup
    installs the four http hooks, says SessionStart is not captured and how to fix it, and
    status says so too."""
    spaced = tmp_path / "Jane Doe" / "python3"
    monkeypatch.setattr(sys, "executable", str(spaced))  # never run: no probe, no relay
    code, out = invoke("setup")
    assert code == 0, out
    assert "SessionStart is not captured" in out
    assert "virtualenv" in out
    doc = json.loads(_default_path(env).read_text())
    assert "SessionStart" not in doc["hooks"]
    for event in ("PreToolUse", "PostToolUse", "PostToolUseFailure", "SessionEnd"):
        assert _hook_entry(doc, event)["type"] == "http"
    payload = json.loads(invoke("status", "--json")[1])
    assert payload["session_start_captured"] is False
    assert any("SessionStart is not captured" in w for w in payload["warnings"])


def _fake_claude(tmp_path: Path, version: str) -> Path:
    exe = tmp_path / "bin" / "claude"
    exe.parent.mkdir(exist_ok=True)
    exe.write_text(f"#!{sys.executable}\nprint('{version} (Claude Code)')\n")
    exe.chmod(0o700)
    return exe


@pytest.mark.parametrize(
    ("version", "warned"), [("2.1.200", True), ("2.1.281", False), ("2.1.284", False)]
)
def test_status_reports_the_claude_code_version_and_warns_below_the_exec_form_minimum(
    env: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: str,
    warned: bool,
) -> None:
    """A Claude Code without ``args`` support would run ``command`` alone through a shell:
    the relay would never run."""
    invoke("setup")
    monkeypatch.setenv("AGENTTAPE_CLAUDE_EXECUTABLE", str(_fake_claude(tmp_path, version)))
    code, out = invoke("status", "--json")
    assert code == 0, out
    payload = json.loads(out)
    assert payload["claude_code"] == {
        "version": version,
        "exec_form_min_version": "2.1.281",
        "exec_form_supported": not warned,
    }
    older = [w for w in payload["warnings"] if "2.1.281" in w]
    assert bool(older) is warned
    _code, text = invoke("status")
    assert f"Claude Code: {version}" in text


def _script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(0o700)
    return path


def test_status_bounds_the_probe_and_the_version_check_at_three_seconds_each(
    env: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scripted `hooks status --json` must not hang on a slow interpreter or a slow
    `claude --version`."""
    import time

    invoke("setup")
    slow = str(_script(tmp_path / "bin" / "python3", "import time\ntime.sleep(30)\n"))
    monkeypatch.setattr(sys, "executable", slow)
    path = _default_path(env)
    doc = json.loads(path.read_text())
    doc["hooks"]["SessionStart"][0]["hooks"][0]["command"] = slow
    path.write_text(json.dumps(doc))
    os.chmod(path, 0o600)
    claude = _script(tmp_path / "slow" / "claude", "import time\ntime.sleep(30)\n")
    monkeypatch.setenv("AGENTTAPE_CLAUDE_EXECUTABLE", str(claude))
    started = time.monotonic()
    payload = json.loads(invoke("status", "--json")[1])
    elapsed = time.monotonic() - started
    assert elapsed < 9, elapsed
    assert payload["relay"]["interpreter_runs_relay"] is False
    assert any("within 3s" in w for w in payload["warnings"])
    assert payload["claude_code"]["version"] is None


def test_status_no_probe_runs_nothing(
    env: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agenttape.cli.commands import hooks as hooks_cmd

    invoke("setup")
    marker = tmp_path / "claude-ran"
    claude = _script(
        tmp_path / "bin" / "claude",
        f"open({str(marker)!r}, 'w').close()\nprint('2.1.284 (Claude Code)')\n",
    )
    monkeypatch.setenv("AGENTTAPE_CLAUDE_EXECUTABLE", str(claude))

    def no_probe(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the relay probe ran")

    monkeypatch.setattr(hooks_cmd, "_relay_probe_failure", no_probe)
    code, out = invoke("status", "--json", "--no-probe")
    assert code == 0, out
    payload = json.loads(out)
    assert not marker.exists()
    assert payload["checks_run"] is False
    assert payload["relay"]["interpreter_runs_relay"] is None
    assert payload["claude_code"]["version"] is None
    assert payload["warnings"] == []


def test_status_says_nothing_about_a_claude_code_it_cannot_find(env: dict[str, Path]) -> None:
    invoke("setup")
    payload = json.loads(invoke("status", "--json")[1])
    assert payload["claude_code"] == {
        "version": None,
        "exec_form_min_version": "2.1.281",
        "exec_form_supported": None,
    }
    assert payload["warnings"] == []


def test_setup_and_remove_leave_another_agenttapes_relay_alone(
    env: dict[str, Path], tmp_path: Path
) -> None:
    """A dev instance's relay (another data dir) is not this install's to replace."""
    other = {
        "type": "command",
        "command": sys.executable,
        "args": [
            "-I",
            "-m",
            "agenttape.cli.hook_relay",
            "--data-dir",
            str(tmp_path / "dev-data"),
            "--url",
            f"http://127.0.0.1:9999{HOOK_URL_PATH}",
        ],
        "timeout": 10,
    }
    path = _default_path(env)
    path.parent.mkdir(parents=True)
    original = {"hooks": {"SessionStart": [{"hooks": [other]}]}}
    path.write_text(json.dumps(original))
    os.chmod(path, 0o600)

    code, out = invoke("setup")
    assert code == 0, out
    start = json.loads(path.read_text())["hooks"]["SessionStart"]
    assert start[0] == {"hooks": [other]}
    assert len(start) == 2
    payload = json.loads(invoke("status", "--json")[1])
    assert payload["relay"]["interpreter"] == sys.executable
    assert payload["warnings"] == []
    assert payload["other_relays"] == [
        {"interpreter": sys.executable, "data_dir": str(tmp_path / "dev-data")}
    ]
    _code, text = invoke("status")
    assert f"Another AgentTape's relay (data dir {tmp_path / 'dev-data'})" in text

    code, out = invoke("remove")
    assert code == 0, out
    assert "Removed 5" in out
    assert json.loads(path.read_text()) == original


def test_remove_takes_out_the_relay_and_its_token_copy(env: dict[str, Path]) -> None:
    invoke("setup")
    copy_path = env["data_dir"] / "capture" / "hooks-client.token"
    assert copy_path.exists()
    code, out = invoke("remove")
    assert code == 0, out
    assert "Removed 5" in out
    assert not copy_path.exists()
    code, out = invoke("status", "--json")
    assert json.loads(out)["events"] == []


def test_status_json_reports_events_url_timeouts_and_token(env: dict[str, Path]) -> None:
    invoke("setup")
    code, out = invoke("status", "--json")
    assert code == 0, out
    payload = json.loads(out)
    assert set(payload["events"]) == {
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "SessionStart",
        "SessionEnd",
    }
    assert payload["timeouts"] == [3, 10]
    assert payload["token_valid"] is True
    assert payload["relay"] == {
        "events": ["SessionStart"],
        "interpreter": sys.executable,
        "interpreter_exists": True,
        "interpreter_runs_relay": True,
        "token_copy_valid": True,
        "last_outcome": None,
    }
    assert payload["http_events_claude_code_skips"] == []
    assert payload["warnings"] == []


def test_status_shows_the_relays_last_outcome(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whether the relay ran, and how it ended, without a token or a payload."""
    import subprocess

    monkeypatch.setenv("AGENTTAPE_PORT", "9")  # nothing listens on the discard port
    invoke("setup")
    _code, text = invoke("status")
    assert "Relay last run: not recorded yet" in text
    start = _hook_entry(json.loads(_default_path(env).read_text()), "SessionStart")
    proc = subprocess.run(
        _relay_argv(start),
        input=b'{"hook_event_name": "SessionStart"}',
        capture_output=True,
        timeout=8,
        check=False,
    )
    assert proc.returncode == 0
    payload = json.loads(invoke("status", "--json")[1])
    last = payload["relay"]["last_outcome"]
    assert last["outcome"] == "unreachable"
    assert last["http_status"] is None
    _code, text = invoke("status")
    assert f"Relay last run: {last['at']} unreachable" in text


def test_status_warns_about_a_group_or_world_readable_settings_file(env: dict[str, Path]) -> None:
    invoke("setup")
    os.chmod(_default_path(env), 0o644)
    code, out = invoke("status", "--json")
    assert code == 0, out
    payload = json.loads(out)
    assert any("readable by group or others" in w for w in payload["warnings"])


def test_end_to_end_the_written_token_authenticates_against_the_hooks_endpoint(
    env: dict[str, Path], tmp_path: Path
) -> None:
    invoke("setup")
    doc = json.loads(_default_path(env).read_text())
    token = _bearer(_hook_entry(doc, "PreToolUse"))

    settings = make_settings(tmp_path, data_dir=env["data_dir"])
    ctx = build_context(settings, migrate=True)
    client = loopback_client(ctx)
    body = {
        "hook_event_name": "SessionStart",
        "session_id": "sess-cli-e2e",
        "cwd": str(tmp_path / "repos"),
        "source": "startup",
        "transcript_path": "/x/transcript.jsonl",
    }
    ok = post_hook(client, token, body)
    assert ok.status_code == 200
    assert ok.json() == {}

    bad = post_hook(client, "atc_" + "made-up-token", body)
    assert bad.status_code == 401
