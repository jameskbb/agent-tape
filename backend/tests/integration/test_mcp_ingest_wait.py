"""An interactive MCP finding waits for the hook ingest thread only so long, and says
honestly whether it was recorded.

The finding is written on the hook ingest thread, after every hook already received. That
thread can be busy (a queue of hooks, a locked database), and the stdio MCP server gives
up after its own HTTP timeout. So the route bounds its wait: a finding still queued is
cancelled and answered 503 "not recorded"; one already being written is waited for a
little longer, then answered 503 "may have been recorded". Recording the identical finding
again is always safe: AgentTape keeps one copy. A session's run-terminal hooks (automatic
verification) never run on the ingest thread, so one session ending never holds up
another session's hooks or findings.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agenttape.api.routers import mcp as mcp_router
from agenttape.application.context import AppContext
from agenttape.application.schemas.capture import FindingRecordIn
from agenttape.application.services.capture import CaptureService
from agenttape.application.services.hook_ingest import hook_ingest
from agenttape.infrastructure.db.models import Event
from agenttape.mcp import client as mcp_client
from agenttape.mcp.client import CaptureClient, CaptureClientConfig, CaptureError
from agenttape.worker import hooks as worker_hooks
from tests.integration.capture_helpers import (
    hooks_token,
    loopback_client,
    mcp_token,
    post_hook,
    session_end,
    session_start,
    svc_project,
)

FINDING = {
    "kind": "claim",
    "text": "greet() is defined on line 1",
    "citations": [
        {"path": "src/greeter.py", "line_start": 1, "line_end": 1, "quote": "def greet():"}
    ],
}


def _post(client: TestClient, token: str, cwd: str) -> Any:
    return client.post(
        "/api/v1/mcp/findings",
        json={**FINDING, "cwd": cwd, "instance_id": "inst-wait"},
        headers={"content-type": "application/json", "authorization": f"Bearer {token}"},
    )


def _findings(ctx: AppContext) -> list[Event]:
    with ctx.session() as session:
        return list(session.scalars(select(Event).where(Event.type == "finding")))


def test_the_server_gives_up_well_before_the_mcp_client_does() -> None:
    assert (
        mcp_router.MCP_QUEUE_WAIT_SECONDS + mcp_router.MCP_WRITE_WAIT_SECONDS
        < mcp_client._TIMEOUT_SECONDS
    )
    # The finding's own write can wait out one SQLite busy timeout once it has started.
    assert mcp_router.MCP_WRITE_WAIT_SECONDS > 10.0


def test_a_finding_still_queued_when_the_wait_ends_is_not_recorded(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp_router, "MCP_QUEUE_WAIT_SECONDS", 0.2)
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    ingest = hook_ingest(ctx)
    gate = threading.Event()
    held = ingest.in_order(gate.wait)
    # Only ends a broken test: a route that waits unbounded is answered once this fires.
    safety = threading.Timer(5.0, gate.set)
    safety.start()
    try:
        response = _post(client, mcp_token(ctx), str(repo))
    finally:
        gate.set()
        safety.cancel()
    held.result(timeout=10)
    assert ingest.drain()
    assert response.status_code == 503, response.text
    error = response.json()["error"]
    assert error["code"] == "ingest_busy"
    assert "was not recorded" in error["message"]
    assert _findings(ctx) == []


def test_a_finding_already_being_written_is_reported_as_maybe_recorded_and_a_retry_keeps_one(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp_router, "MCP_QUEUE_WAIT_SECONDS", 0.1)
    monkeypatch.setattr(mcp_router, "MCP_WRITE_WAIT_SECONDS", 0.2)
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = mcp_token(ctx)
    started = threading.Event()
    gate = threading.Event()
    real = CaptureService.record_interactive_finding

    def slow(self: CaptureService, record: FindingRecordIn) -> Any:
        started.set()
        gate.wait(timeout=10)
        return real(self, record)

    monkeypatch.setattr(CaptureService, "record_interactive_finding", slow)
    try:
        response = _post(client, token, str(repo))
    finally:
        gate.set()
    assert started.is_set()
    assert hook_ingest(ctx).drain()
    assert response.status_code == 503, response.text
    error = response.json()["error"]
    assert error["code"] == "finding_write_pending"
    assert "may have been recorded" in error["message"]
    [recorded] = _findings(ctx)

    monkeypatch.setattr(CaptureService, "record_interactive_finding", real)
    retry = _post(client, token, str(repo))
    assert retry.status_code == 201, retry.text
    assert retry.json()["event_id"] == recorded.id
    assert [e.id for e in _findings(ctx)] == [recorded.id]


def test_a_different_finding_is_never_folded_into_an_earlier_one(ctx: AppContext) -> None:
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = mcp_token(ctx)
    first = _post(client, token, str(repo))
    other = client.post(
        "/api/v1/mcp/findings",
        json={**FINDING, "text": "another claim", "cwd": str(repo), "instance_id": "inst-wait"},
        headers={"content-type": "application/json", "authorization": f"Bearer {token}"},
    )
    assert first.status_code == other.status_code == 201
    assert first.json()["event_id"] != other.json()["event_id"]
    assert len(_findings(ctx)) == 2


def test_a_session_ending_never_holds_up_the_ingest_thread(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``SessionEnd`` closes an observed run and fires its run-terminal hooks (context
    ingest, verification of every citation): those run beside the ingest thread, so the
    next hook or finding from any session is not queued behind them."""
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    hooks = hooks_token(ctx)
    started = threading.Event()
    gate = threading.Event()

    async def blocking_terminal_hook(_ctx: AppContext, _run_id: str, _status: str) -> None:
        started.set()
        gate.wait(timeout=10)

    monkeypatch.setattr(worker_hooks, "_on_terminal", [blocking_terminal_hook])
    ingest = hook_ingest(ctx)
    try:
        for body in (
            session_start(str(repo), session_id="sess-ending"),
            session_end(str(repo), session_id="sess-ending"),
        ):
            assert post_hook(client, hooks, body, drain=False).status_code == 200
        assert started.wait(timeout=10)
        # The ingest thread is free while the terminal hook still runs.
        assert ingest.in_order(lambda: "free").result(timeout=5) == "free"
        assert ingest.drain(timeout=0.05) is False  # drain still waits for the terminal hook
    finally:
        gate.set()
    assert ingest.drain()


def _config() -> CaptureClientConfig:
    return CaptureClientConfig(
        api_base="http://ignored",
        token="atc_token",
        attempt_id="",
        run_id="",
        instance_id="instance",
        cwd="/work",
    )


def test_the_client_passes_a_503_through_as_said() -> None:
    def busy(url: str, body: bytes, headers: Mapping[str, str]) -> tuple[int, bytes]:
        return 503, b'{"error": {"code": "finding_write_pending", "message": "M may be."}}'

    with pytest.raises(CaptureError) as caught:
        CaptureClient(_config(), transport=busy).post_finding(
            FindingRecordIn(kind="claim", text="a finding")
        )
    assert str(caught.value) == "M may be."


def test_a_client_timeout_never_claims_the_finding_was_not_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Opener:
        def open(self, *_args: object, **_kwargs: object) -> None:
            raise TimeoutError("timed out")

    monkeypatch.setattr(mcp_client, "build_opener", lambda *_a: _Opener())
    with pytest.raises(CaptureError) as caught:
        CaptureClient(_config()).post_finding(FindingRecordIn(kind="claim", text="a finding"))
    message = str(caught.value)
    assert "not recorded" not in message
    assert "may have been recorded" in message
    assert "safe" in message
