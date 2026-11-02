"""Claude Code ``http`` hooks ingest: an interactive session becomes an observed run with
tool events and context evidence, with no worker involved."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from agenttape.application.context import AppContext
from agenttape.application.services.observed_sessions import ObservedSessionService
from agenttape.capture.claude_hooks import parse_hook, session_hash
from agenttape.infrastructure.db.extra_models.context_evidence import ContextEvidenceEvent
from agenttape.infrastructure.db.extra_models.file_evidence import FileEvidenceEvent, RunFileSummary
from agenttape.infrastructure.db.extra_models.observed_sessions import ObservedSession
from agenttape.security.capture import CaptureTokenStore, mint_attempt_token
from tests.integration.capture_helpers import (
    TRANSCRIPT_PATH,
    hooks_token,
    loopback_client,
    post_hook,
    post_read,
    post_tool_use,
    pre_read,
    session_end,
    session_start,
    svc_project,
)

SESSION_ID = "sess-7f3a9c-unique"
#: A fake AWS-shaped secret, assembled at runtime so the secret scan does not flag the test.
LEAK = "AKIA" + "QQQQWWWWEEEERRRR"


def test_an_interactive_session_becomes_an_observed_run(ctx: AppContext) -> None:
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)
    file_path = str(repo / "src" / "greeter.py")

    r1 = post_hook(client, token, session_start(str(repo), session_id=SESSION_ID))
    assert r1.status_code == 200 and r1.json() == {}
    r2 = post_hook(client, token, pre_read(str(repo), file_path, session_id=SESSION_ID))
    assert r2.status_code == 200 and r2.json() == {}
    r3 = post_hook(client, token, post_read(str(repo), file_path, session_id=SESSION_ID))
    assert r3.status_code == 200 and r3.json() == {}

    runs = client.get("/api/v1/runs").json()["items"]
    assert len(runs) == 1
    run = runs[0]
    assert run["kind"] == "observed"
    assert run["source"] == "hook"
    assert run["status"] == "running"
    assert run["adapter_type"] == "claude-code"
    assert run["project_id"] == _project_id

    events = client.get(f"/api/v1/runs/{run['id']}/events").json()["items"]
    types = [e["type"] for e in events]
    assert "lifecycle" in types
    assert "tool_call" in types
    assert "tool_result" in types
    call = next(e for e in events if e["type"] == "tool_call")
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["parent_event_id"] == call["id"]
    assert call["sequence"] < result["sequence"]

    with ctx.session() as session:
        rows = session.scalars(
            select(ContextEvidenceEvent).where(
                ContextEvidenceEvent.run_id == run["id"],
                ContextEvidenceEvent.source_key == "file:src/greeter.py",
                ContextEvidenceEvent.stage == "retrieved",
            )
        ).all()
        assert len(rows) == 1


def test_session_outside_every_project_is_ignored(ctx: AppContext, tmp_path: Path) -> None:
    svc_project(ctx)
    outside = tmp_path / "not-a-project"
    outside.mkdir()
    client = loopback_client(ctx)
    token = hooks_token(ctx)

    r = post_hook(client, token, session_start(str(outside), session_id="sess-outside"))
    assert r.status_code == 200 and r.json() == {}

    assert client.get("/api/v1/runs").json()["items"] == []
    with ctx.session() as session:
        assert session.scalars(select(ObservedSession)).first() is None


def test_hooks_need_the_hooks_capture_token(ctx: AppContext) -> None:
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)
    body = session_start(str(repo), session_id="sess-token-a")

    missing = post_hook(client, "", body)
    assert missing.status_code == 401

    wrong = post_hook(client, "atc_wrong-token-value-not-real", body)
    assert wrong.status_code == 401

    attempt_id = "11111111-1111-1111-1111-111111111111"
    attempt_token = mint_attempt_token(ctx.settings.data_dir, attempt_id)
    cross_type = post_hook(client, attempt_token, body)
    assert cross_type.status_code == 401

    ok = post_hook(client, token, body)
    assert ok.status_code == 200 and ok.json() == {}

    second = post_hook(client, token, session_start(str(repo), session_id="sess-token-b"))
    assert second.status_code == 200 and second.json() == {}
    runs = client.get("/api/v1/runs").json()["items"]
    assert len(runs) == 2


def test_session_id_and_transcript_path_are_never_stored(ctx: AppContext) -> None:
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)
    file_path = str(repo / "src" / "greeter.py")

    post_hook(client, token, session_start(str(repo), session_id=SESSION_ID))
    post_hook(client, token, pre_read(str(repo), file_path, session_id=SESSION_ID))
    post_hook(client, token, post_read(str(repo), file_path, session_id=SESSION_ID))

    runs = client.get("/api/v1/runs").json()["items"]
    run_id = runs[0]["id"]
    detail = client.get(f"/api/v1/runs/{run_id}").json()
    events = client.get(f"/api/v1/runs/{run_id}/events").json()["items"]

    haystacks = [json.dumps(detail), json.dumps(events)]
    for haystack in haystacks:
        assert SESSION_ID not in haystack
        assert TRANSCRIPT_PATH not in haystack

    with ctx.session() as session:
        row = session.scalars(select(ObservedSession)).one()
        assert row.session_hash == session_hash(SESSION_ID)
        row_dict = {k: v for k, v in vars(row).items() if not k.startswith("_")}
        assert SESSION_ID not in json.dumps(row_dict, default=str)
        assert TRANSCRIPT_PATH not in json.dumps(row_dict, default=str)


def test_redelivered_hooks_do_not_duplicate_events(ctx: AppContext) -> None:
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)
    file_path = str(repo / "src" / "greeter.py")

    post_hook(client, token, session_start(str(repo), session_id=SESSION_ID))
    post_hook(client, token, pre_read(str(repo), file_path, session_id=SESSION_ID))
    post_hook(client, token, post_read(str(repo), file_path, session_id=SESSION_ID))
    post_hook(client, token, post_read(str(repo), file_path, session_id=SESSION_ID))  # redelivered

    runs = client.get("/api/v1/runs").json()["items"]
    run_id = runs[0]["id"]
    events = client.get(f"/api/v1/runs/{run_id}/events").json()["items"]
    results = [e for e in events if e["type"] == "tool_result"]
    assert len(results) == 1


def test_post_without_pre_backfills_the_call(ctx: AppContext) -> None:
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)
    file_path = str(repo / "src" / "greeter.py")

    post_hook(client, token, session_start(str(repo), session_id=SESSION_ID))
    post_hook(
        client, token, post_read(str(repo), file_path, session_id=SESSION_ID)
    )  # no PreToolUse first

    runs = client.get("/api/v1/runs").json()["items"]
    run_id = runs[0]["id"]
    events = client.get(f"/api/v1/runs/{run_id}/events").json()["items"]
    calls = [e for e in events if e["type"] == "tool_call"]
    results = [e for e in events if e["type"] == "tool_result"]
    assert len(calls) == 1
    assert len(results) == 1
    assert results[0]["parent_event_id"] == calls[0]["id"]


def test_capture_token_store_keeps_only_hashes(ctx: AppContext) -> None:
    store = CaptureTokenStore(ctx.settings.data_dir)
    token = store.mint("hooks")
    tokens_path = ctx.settings.data_dir / "capture" / "tokens.json"
    assert tokens_path.exists()
    mode = tokens_path.stat().st_mode & 0o777
    assert mode == 0o600
    raw = tokens_path.read_text(encoding="utf-8")
    assert token not in raw

    assert store.verify("hooks", token) is True
    assert store.verify("hooks", "atc_not-the-real-token") is False

    rotated = store.mint("hooks")
    assert rotated != token
    assert store.verify("hooks", token) is False
    assert store.verify("hooks", rotated) is True

    assert store.revoke("hooks") is True
    assert store.verify("hooks", rotated) is False
    assert store.revoke("hooks") is False


def _spy_terminal_hooks(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Installs a spy in place of every registered run-terminal hook (through monkeypatch,
    so the real registry is restored after the test)."""
    from agenttape.worker import hooks as worker_hooks

    calls: list[tuple[str, str]] = []

    async def spy(_ctx: AppContext, run_id: str, status: str) -> None:
        calls.append((run_id, status))

    monkeypatch.setattr(worker_hooks, "_on_terminal", [spy])
    return calls


def test_session_end_closes_the_run_succeeded(ctx: AppContext) -> None:
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)

    post_hook(client, token, session_start(str(repo), session_id=SESSION_ID))
    run_id = client.get("/api/v1/runs").json()["items"][0]["id"]

    r = post_hook(
        client, token, session_end(str(repo), reason="prompt_input_exit", session_id=SESSION_ID)
    )
    assert r.status_code == 200 and r.json() == {}

    detail = client.get(f"/api/v1/runs/{run_id}").json()
    assert detail["run"]["status"] == "succeeded"
    assert detail["run"]["finished_at"] is not None
    assert detail["run"]["duration_ms"] is not None
    assert detail["outputs"]["closed_by"] == "session_end"
    assert detail["outputs"]["end_reason"] == "prompt_input_exit"
    assert detail["attempts"][0]["status"] == "succeeded"

    events = client.get(f"/api/v1/runs/{run_id}/events").json()["items"]
    assert any(e["type"] == "lifecycle" and "ended" in e["summary"].lower() for e in events)

    with ctx.session() as session:
        row = session.scalars(select(ObservedSession).where(ObservedSession.run_id == run_id)).one()
        assert row.status == "closed"
        assert row.close_reason == "session_end"


def test_session_end_fires_the_run_terminal_hooks_once(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _spy_terminal_hooks(monkeypatch)
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)

    post_hook(client, token, session_start(str(repo), session_id=SESSION_ID))
    run_id = client.get("/api/v1/runs").json()["items"][0]["id"]
    post_hook(client, token, session_end(str(repo), session_id=SESSION_ID))

    # BackgroundTasks run within the TestClient's request/response cycle - no extra wait.
    assert calls == [(run_id, "succeeded")]


def test_idle_row_closes_as_timed_out_and_a_second_sweep_is_a_noop(ctx: AppContext) -> None:
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)

    post_hook(client, token, session_start(str(repo), session_id=SESSION_ID))
    run_id = client.get("/api/v1/runs").json()["items"][0]["id"]

    with ctx.session() as session:
        row = session.scalars(select(ObservedSession).where(ObservedSession.run_id == run_id)).one()
        row.last_seen_at = ctx.clock.now() - timedelta(hours=2)
        session.flush()

    closed = ObservedSessionService(ctx).sweep_idle()
    assert closed == [(run_id, "timed_out")]

    detail = client.get(f"/api/v1/runs/{run_id}").json()
    assert detail["run"]["status"] == "timed_out"
    assert detail["run"]["failure_category"] == "idle_timeout"
    assert "not" in (detail["run"]["failure_message"] or "").lower()

    with ctx.session() as session:
        row = session.scalars(select(ObservedSession).where(ObservedSession.run_id == run_id)).one()
        assert row.status == "closed"
        assert row.close_reason == "idle_window"

    assert ObservedSessionService(ctx).sweep_idle() == []


def test_hook_after_close_starts_a_new_run_with_the_same_source_ref(ctx: AppContext) -> None:
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)

    post_hook(client, token, session_start(str(repo), session_id=SESSION_ID))
    first = client.get("/api/v1/runs").json()["items"][0]
    post_hook(client, token, session_end(str(repo), session_id=SESSION_ID))

    file_path = str(repo / "src" / "greeter.py")
    post_hook(
        client, token, pre_read(str(repo), file_path, session_id=SESSION_ID)
    )  # new hook after close

    runs = client.get("/api/v1/runs").json()["items"]
    assert len(runs) == 2
    second = next(r for r in runs if r["id"] != first["id"])
    assert second["source_ref"] == first["source_ref"]
    assert second["status"] == "running"


def test_session_end_for_unknown_session_creates_nothing(ctx: AppContext) -> None:
    svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)

    r = post_hook(client, token, session_end("/tmp/wherever", session_id="sess-never-started"))
    assert r.status_code == 200 and r.json() == {}
    assert client.get("/api/v1/runs").json()["items"] == []


def test_observed_sessions_service_is_available_and_sweep_once_fires_hooks(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agenttape.runtime.base import available_services
    from agenttape.runtime.services.observed_sessions import ObservedSessionSweeper

    assert "observed-sessions" in available_services()

    calls = _spy_terminal_hooks(monkeypatch)
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)
    post_hook(client, token, session_start(str(repo), session_id="sess-sweep"))
    run_id = client.get("/api/v1/runs").json()["items"][0]["id"]

    with ctx.session() as session:
        row = session.scalars(select(ObservedSession).where(ObservedSession.run_id == run_id)).one()
        row.last_seen_at = ctx.clock.now() - timedelta(hours=2)
        session.flush()

    sweeper = ObservedSessionSweeper(ctx)
    closed = asyncio.run(sweeper.sweep_once())
    assert closed == [(run_id, "timed_out")]
    assert calls == [(run_id, "timed_out")]


def test_cancel_closes_the_observed_run_at_once(ctx: AppContext) -> None:
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)

    post_hook(client, token, session_start(str(repo), session_id=SESSION_ID))
    run_id = client.get("/api/v1/runs").json()["items"][0]["id"]

    r = client.post(f"/api/v1/runs/{run_id}/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"

    detail = client.get(f"/api/v1/runs/{run_id}").json()
    assert detail["run"]["status"] == "cancelled"
    assert detail["attempts"][0]["status"] == "cancelled"

    events = client.get(f"/api/v1/runs/{run_id}/events").json()["items"]
    cancellation = next(e for e in events if e["type"] == "cancellation")
    assert "not touched" in cancellation["summary"].lower()

    with ctx.session() as session:
        row = session.scalars(select(ObservedSession).where(ObservedSession.run_id == run_id)).one()
        assert row.status == "closed"
        assert row.close_reason == "cancelled"


def test_cancel_fires_the_run_terminal_hooks_too(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SessionEnd, idle sweep, AND cancel all reach the same terminal-hook path (not
    just the first two), so automatic verification runs for a cancelled observed run too."""
    calls = _spy_terminal_hooks(monkeypatch)
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)

    post_hook(client, token, session_start(str(repo), session_id=SESSION_ID))
    run_id = client.get("/api/v1/runs").json()["items"][0]["id"]

    r = client.post(f"/api/v1/runs/{run_id}/cancel")
    assert r.status_code == 200

    assert calls == [(run_id, "cancelled")]


def test_a_later_hook_for_a_cancelled_session_is_ignored(ctx: AppContext) -> None:
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)

    post_hook(client, token, session_start(str(repo), session_id=SESSION_ID))
    run_id = client.get("/api/v1/runs").json()["items"][0]["id"]
    client.post(f"/api/v1/runs/{run_id}/cancel")

    file_path = str(repo / "src" / "greeter.py")
    hook = parse_hook(pre_read(str(repo), file_path, session_id=SESSION_ID))
    assert hook is not None
    outcome = ObservedSessionService(ctx).ingest_hook(hook)
    assert outcome.status == "ignored"
    assert outcome.reason == "observation_cancelled"

    assert len(client.get("/api/v1/runs").json()["items"]) == 1
    events = client.get(f"/api/v1/runs/{run_id}/events").json()["items"]
    assert not any(e["type"] == "tool_call" for e in events)


def test_retry_is_refused_for_observed_runs(ctx: AppContext) -> None:
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)

    post_hook(client, token, session_start(str(repo), session_id=SESSION_ID))
    run_id = client.get("/api/v1/runs").json()["items"][0]["id"]
    client.post(f"/api/v1/runs/{run_id}/cancel")

    r = client.post(f"/api/v1/runs/{run_id}/retry")
    assert r.status_code == 409
    assert "nothing to retry" in r.json()["error"]["message"]


def test_secret_shaped_cwd_and_details_are_redacted(ctx: AppContext) -> None:
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)

    post_hook(client, token, session_start(str(repo), session_id=SESSION_ID))
    run_id = client.get("/api/v1/runs").json()["items"][0]["id"]

    with ctx.session() as session:
        row = session.scalars(select(ObservedSession).where(ObservedSession.run_id == run_id)).one()
        row.cwd = f"/tmp/{LEAK}/workspace"
        row.details = {**(row.details or {}), "planted": LEAK}
        session.flush()
        session.refresh(row)
        assert LEAK not in (row.cwd or "")
        assert LEAK not in json.dumps(row.details)


def test_replay_and_fork_are_refused_for_observed_runs(ctx: AppContext) -> None:
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)

    post_hook(client, token, session_start(str(repo), session_id=SESSION_ID))
    run_id = client.get("/api/v1/runs").json()["items"][0]["id"]

    assert client.post(f"/api/v1/runs/{run_id}/replay").status_code == 409
    assert client.post(f"/api/v1/runs/{run_id}/fork", json={}).status_code == 409


def test_read_tool_result_renders_the_cat_n_layout_with_the_reported_start_line(
    ctx: AppContext,
) -> None:
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)
    file_path = str(repo / "src" / "greeter.py")

    post_hook(client, token, session_start(str(repo), session_id=SESSION_ID))
    post_hook(
        client,
        token,
        post_tool_use(
            str(repo),
            tool_name="Read",
            tool_use_id="toolu_R5",
            tool_input={"file_path": file_path, "offset": 5},
            tool_response={"file": {"content": "a\nb\n", "startLine": 5}},
            session_id=SESSION_ID,
        ),
    )

    run_id = client.get("/api/v1/runs").json()["items"][0]["id"]
    events = client.get(f"/api/v1/runs/{run_id}/events").json()["items"]
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["payload"]["output"] == "     5\ta\n     6\tb"
    assert result["payload"]["response_shape"] == "read_file"

    with ctx.session() as session:
        row = session.scalars(
            select(ContextEvidenceEvent).where(
                ContextEvidenceEvent.run_id == run_id,
                ContextEvidenceEvent.source_key == "file:src/greeter.py",
            )
        ).one()
        assert row.details.get("returned_lines") == {"start": 5, "end": 6}


def test_read_tool_result_falls_back_to_offset_then_to_one(ctx: AppContext) -> None:
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)
    file_path = str(repo / "src" / "greeter.py")

    post_hook(client, token, session_start(str(repo), session_id=SESSION_ID))
    post_hook(
        client,
        token,
        post_tool_use(
            str(repo),
            tool_name="Read",
            tool_use_id="toolu_R6",
            tool_input={"file_path": file_path, "offset": 3},
            tool_response={"file": {"content": "x\ny\n"}},
            session_id=SESSION_ID,
        ),
    )
    post_hook(
        client,
        token,
        post_tool_use(
            str(repo),
            tool_name="Read",
            tool_use_id="toolu_R7",
            tool_input={"file_path": file_path},
            tool_response={"file": {"content": "x\ny\n"}},
            session_id=SESSION_ID,
        ),
    )

    run_id = client.get("/api/v1/runs").json()["items"][0]["id"]
    events = client.get(f"/api/v1/runs/{run_id}/events").json()["items"]
    results = [e for e in events if e["type"] == "tool_result"]
    outputs = {r["payload"]["output"] for r in results}
    assert "     3\tx\n     4\ty" in outputs
    assert "     1\tx\n     2\ty" in outputs


def test_non_dict_tool_responses_are_captured_as_text_or_json(ctx: AppContext) -> None:
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)

    post_hook(client, token, session_start(str(repo), session_id=SESSION_ID))
    post_hook(
        client,
        token,
        post_tool_use(
            str(repo),
            tool_name="Glob",
            tool_use_id="toolu_G1",
            tool_input={"pattern": "*.py"},
            tool_response="src/greeter.py",
            session_id=SESSION_ID,
        ),
    )
    post_hook(
        client,
        token,
        post_tool_use(
            str(repo),
            tool_name="SomeOtherTool",
            tool_use_id="toolu_O1",
            tool_input={},
            tool_response={"weird": ["shape"]},
            session_id=SESSION_ID,
        ),
    )

    run_id = client.get("/api/v1/runs").json()["items"][0]["id"]
    events = client.get(f"/api/v1/runs/{run_id}/events").json()["items"]
    results = {
        r["payload"]["tool_use_id"]: r["payload"] for r in events if r["type"] == "tool_result"
    }
    assert results["toolu_G1"]["response_shape"] == "text"
    assert results["toolu_G1"]["output"] == "src/greeter.py"
    assert results["toolu_O1"]["response_shape"] == "json"
    assert "weird" in results["toolu_O1"]["output"]


def test_bash_tool_result_surfaces_stdout_and_stderr(ctx: AppContext) -> None:
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)

    post_hook(client, token, session_start(str(repo), session_id=SESSION_ID))
    post_hook(
        client,
        token,
        post_tool_use(
            str(repo),
            tool_name="Bash",
            tool_use_id="toolu_B1",
            tool_input={"command": "echo hi"},
            tool_response={"stdout": "x\n", "stderr": "", "interrupted": False},
            session_id=SESSION_ID,
        ),
    )

    run_id = client.get("/api/v1/runs").json()["items"][0]["id"]
    events = client.get(f"/api/v1/runs/{run_id}/events").json()["items"]
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["payload"]["output"] == "x\n"
    assert result["payload"]["response_shape"] == "bash"
    assert "stderr" not in result["payload"]

    post_hook(
        client,
        token,
        post_tool_use(
            str(repo),
            tool_name="Bash",
            tool_use_id="toolu_B2",
            tool_input={"command": "echo hi 1>&2"},
            tool_response={"stdout": "", "stderr": "oops\n", "interrupted": False},
            session_id=SESSION_ID,
        ),
    )
    events = client.get(f"/api/v1/runs/{run_id}/events").json()["items"]
    second = next(
        e
        for e in events
        if e["type"] == "tool_result" and e["payload"].get("tool_use_id") == "toolu_B2"
    )
    assert second["payload"]["stderr"] == "oops\n"


def test_bash_shell_reads_become_context_and_file_evidence_for_observed_runs(
    ctx: AppContext,
) -> None:
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)

    post_hook(client, token, session_start(str(repo), session_id=SESSION_ID))
    post_hook(
        client,
        token,
        post_tool_use(
            str(repo),
            tool_name="Bash",
            tool_use_id="toolu_S1",
            tool_input={"command": "sed -n '1,2p' src/greeter.py"},
            tool_response={
                "stdout": "def greet():\n    return 'hi'\n",
                "stderr": "",
                "interrupted": False,
            },
            session_id=SESSION_ID,
        ),
    )
    post_hook(
        client,
        token,
        post_tool_use(
            str(repo),
            tool_name="Bash",
            tool_use_id="toolu_S2",
            tool_input={"command": "cat src/greeter.py | head"},
            tool_response={"stdout": "def greet():\n", "stderr": "", "interrupted": False},
            session_id=SESSION_ID,
        ),
    )

    run_id = client.get("/api/v1/runs").json()["items"][0]["id"]
    with ctx.session() as session:
        context_rows = session.scalars(
            select(ContextEvidenceEvent).where(
                ContextEvidenceEvent.run_id == run_id,
                ContextEvidenceEvent.source_key == "file:src/greeter.py",
            )
        ).all()
        assert len(context_rows) == 1
        assert context_rows[0].details.get("shell_read") is not None

        file_rows = session.scalars(
            select(FileEvidenceEvent).where(
                FileEvidenceEvent.run_id == run_id, FileEvidenceEvent.path == "src/greeter.py"
            )
        ).all()
        assert len(file_rows) == 1
        assert file_rows[0].operation == "read"
        assert file_rows[0].source == "tool_event"


def test_bash_shell_read_resolves_relative_to_the_reported_cwd(ctx: AppContext) -> None:
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)

    post_hook(client, token, session_start(str(repo), session_id=SESSION_ID))
    post_hook(
        client,
        token,
        post_tool_use(
            str(repo / "src"),
            tool_name="Bash",
            tool_use_id="toolu_S3",
            tool_input={"command": "cat greeter.py"},
            tool_response={"stdout": "def greet():\n", "stderr": "", "interrupted": False},
            session_id=SESSION_ID,
        ),
    )

    run_id = client.get("/api/v1/runs").json()["items"][0]["id"]
    with ctx.session() as session:
        file_rows = session.scalars(
            select(FileEvidenceEvent).where(
                FileEvidenceEvent.run_id == run_id, FileEvidenceEvent.path == "src/greeter.py"
            )
        ).all()
        assert len(file_rows) == 1


def test_post_tool_use_failure_records_the_error_as_is_error(ctx: AppContext) -> None:
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)
    file_path = str(repo / "src" / "greeter.py")

    post_hook(client, token, session_start(str(repo), session_id=SESSION_ID))
    body = {
        "hook_event_name": "PostToolUseFailure",
        "session_id": SESSION_ID,
        "cwd": str(repo),
        "transcript_path": TRANSCRIPT_PATH,
        "tool_name": "Read",
        "tool_use_id": "toolu_F1",
        "tool_input": {"file_path": file_path},
        "error": "ENOENT: no such file",
        "is_interrupt": False,
    }
    r = post_hook(client, token, body)
    assert r.status_code == 200 and r.json() == {}

    run_id = client.get("/api/v1/runs").json()["items"][0]["id"]
    events = client.get(f"/api/v1/runs/{run_id}/events").json()["items"]
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["payload"]["is_error"] is True
    assert result["payload"]["output"] == "ENOENT: no such file"
    assert result["payload"]["is_interrupt"] is False

    with ctx.session() as session:
        row = session.scalars(
            select(ContextEvidenceEvent).where(
                ContextEvidenceEvent.run_id == run_id,
                ContextEvidenceEvent.source_key == "file:src/greeter.py",
            )
        ).one()
        assert row.stage == "unavailable"


def test_session_end_leaves_file_evidence_summaries_for_the_observed_run(
    ctx: AppContext,
) -> None:
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)
    file_path = str(repo / "src" / "greeter.py")

    post_hook(client, token, session_start(str(repo), session_id=SESSION_ID))
    post_hook(client, token, pre_read(str(repo), file_path, session_id=SESSION_ID))
    post_hook(client, token, post_read(str(repo), file_path, session_id=SESSION_ID))
    run_id = client.get("/api/v1/runs").json()["items"][0]["id"]
    post_hook(client, token, session_end(str(repo), session_id=SESSION_ID))

    with ctx.session() as session:
        summary = session.scalars(
            select(RunFileSummary).where(
                RunFileSummary.run_id == run_id, RunFileSummary.file_key == "src/greeter.py"
            )
        ).one()
        assert summary.classification == "observed_read"
