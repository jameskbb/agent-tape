"""A SessionStart relayed by the ``command`` hook ``agenttape hooks setup`` installs makes
an observed session covered, so ``never_read`` is reachable for it.

Claude Code will not run an ``http`` hook for SessionStart. Without the relay every
observed session's first recorded hook is a tool hook (``joined_mid_session``), and a
missing read can only ever be ``read_not_observed``. This test runs the exact argv
setup wrote into settings.json as a real process (as Claude Code runs an exec-form
``command`` hook), against a real loopback server, and checks the server treats that
SessionStart exactly like an ``http`` one.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agenttape.application.context import AppContext
from agenttape.application.services.hook_ingest import hook_ingest
from agenttape.capture.claude_settings import RELAY_HOOK_TIMEOUT_SECONDS
from agenttape.cli.main import app
from agenttape.infrastructure.db.extra_models.observed_sessions import ObservedSession
from agenttape.infrastructure.db.models import Run
from tests.integration.capture_helpers import (
    live_server,
    loopback_client,
    mcp_token,
    post_hook,
    post_tool_use,
    session_end,
    session_start,
    svc_project,
)
from tests.integration.test_verify_observed_window import _record_finding, _verdict

SESSION_ID = "sess-relayed-start"


def _setup_hooks(
    settings_path: Path, ctx: AppContext, port: int, monkeypatch: pytest.MonkeyPatch
) -> dict[str, object]:
    monkeypatch.setenv("AGENTTAPE_DATA_DIR", str(ctx.settings.data_dir))
    monkeypatch.setenv("AGENTTAPE_PORT", str(port))
    result = CliRunner().invoke(app, ["hooks", "setup", "--settings", str(settings_path)])
    assert result.exit_code == 0, result.output
    doc: dict[str, object] = json.loads(settings_path.read_text())
    return doc


def _entry(doc: dict[str, object], event: str) -> dict[str, object]:
    hooks = doc["hooks"]
    assert isinstance(hooks, dict)
    entry: dict[str, object] = hooks[event][0]["hooks"][0]
    return entry


def _run_command_hook(entry: dict[str, object], stdin: dict[str, object]) -> None:
    """Run a ``command`` hook the way Claude Code runs one in exec form: ``command`` spawned
    directly with ``args`` and the hook input on stdin, no shell."""
    assert entry["type"] == "command"
    args = entry["args"]
    assert isinstance(args, list)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
    proc = subprocess.run(
        [str(entry["command"]), *args],
        input=json.dumps(stdin).encode("utf-8"),
        capture_output=True,
        env=env,
        timeout=RELAY_HOOK_TIMEOUT_SECONDS - 2,  # strictly inside Claude Code's own timeout
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == b""


def test_a_relayed_session_start_makes_an_observed_session_covered(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project_id, repo = svc_project(ctx)
    cwd = str(repo)
    with live_server(ctx) as port:
        doc = _setup_hooks(tmp_path / "claude" / "settings.json", ctx, port, monkeypatch)
        _run_command_hook(_entry(doc, "SessionStart"), session_start(cwd, session_id=SESSION_ID))
        assert hook_ingest(ctx).drain()

        with ctx.session() as session:
            [row] = session.query(ObservedSession).all()
            assert row.details["first_hook"] == "SessionStart"
            assert row.details["first_hook_source"] == "startup"

        # The rest of the session arrives through the http hooks, same token.
        auth = _entry(doc, "PreToolUse")["headers"]
        assert isinstance(auth, dict)
        token = str(auth["Authorization"]).removeprefix("Bearer ")
        client = loopback_client(ctx)
        bash = post_tool_use(
            cwd,
            tool_name="Bash",
            tool_use_id="toolu_B1",
            tool_input={"command": "true"},
            tool_response={"stdout": "", "stderr": "", "interrupted": False},
            session_id=SESSION_ID,
        )
        assert post_hook(client, token, bash).status_code == 200
        run_id = _record_finding(client, mcp_token(ctx), cwd, instance_id="inst-relay")
        assert post_hook(client, token, session_end(cwd, session_id=SESSION_ID)).status_code == 200

    with ctx.session() as session:
        run = session.get(Run, run_id)
        assert run is not None and run.kind == "observed" and run.status == "succeeded"
    verdict = _verdict(ctx, client, run_id)
    assert verdict["verdict"] == "never_read", verdict
    assert verdict["evidence"]["window"]["complete"] is True
    assert verdict["evidence"]["window"]["observed"]["gaps"] == []


def test_without_the_relayed_session_start_the_same_session_joined_mid_session(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: the same session with its SessionStart skipped (what Claude Code does
    to an ``http`` SessionStart hook) is ``read_not_observed``."""
    _project_id, repo = svc_project(ctx)
    cwd = str(repo)
    with live_server(ctx) as port:
        doc = _setup_hooks(tmp_path / "claude" / "settings.json", ctx, port, monkeypatch)
        auth = _entry(doc, "PreToolUse")["headers"]
        assert isinstance(auth, dict)
        token = str(auth["Authorization"]).removeprefix("Bearer ")
        client = loopback_client(ctx)
        bash = post_tool_use(
            cwd,
            tool_name="Bash",
            tool_use_id="toolu_B1",
            tool_input={"command": "true"},
            tool_response={"stdout": "", "stderr": "", "interrupted": False},
            session_id=SESSION_ID,
        )
        assert post_hook(client, token, bash).status_code == 200
        run_id = _record_finding(client, mcp_token(ctx), cwd, instance_id="inst-relay")
        assert post_hook(client, token, session_end(cwd, session_id=SESSION_ID)).status_code == 200

    verdict = _verdict(ctx, client, run_id)
    assert verdict["verdict"] == "read_not_observed", verdict
    assert "joined_mid_session" in verdict["evidence"]["window"]["observed"]["gaps"]
