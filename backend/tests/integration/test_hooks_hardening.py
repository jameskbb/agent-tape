"""The hooks endpoint answers before touching the database, bounds its queue, rate-limits,
and records every gap."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

import agenttape.api.routers.hooks as hooks_router
import agenttape.application.services.hook_ingest as hook_ingest_module
import agenttape.application.services.ingest_state as ingest_state
import tests.integration.capture_helpers as capture_helpers
from agenttape.api.app import create_app
from agenttape.application.context import AppContext
from agenttape.application.services import observed_sessions
from agenttape.capture.claude_hooks import HookEvent, session_hash
from agenttape.infrastructure.db.extra_models.observed_sessions import ObservedSession
from agenttape.infrastructure.db.models import Event
from agenttape.security.capture import CaptureTokenStore
from agenttape.security.ratelimit import RateLimiter
from tests.integration.capture_helpers import (
    hooks_token,
    live_server,
    loopback_client,
    post_hook,
    post_read,
    post_tool_use,
    pre_read,
    running_attempt,
    session_start,
    svc_project,
)


def test_every_2xx_answer_is_exactly_empty_braces(ctx: AppContext) -> None:
    project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)

    known = post_hook(client, token, session_start(str(repo), session_id="sess-2xx-known"))
    assert known.status_code == 200 and known.content == b"{}"

    unknown_event = {
        "hook_event_name": "Notification",
        "session_id": "sess-2xx-unknown",
        "cwd": str(repo),
    }
    unknown = post_hook(client, token, unknown_event)
    assert unknown.status_code == 200 and unknown.content == b"{}"

    outside = post_hook(
        client,
        token,
        session_start("/tmp/definitely-not-a-project", session_id="sess-2xx-outside"),
    )
    assert outside.status_code == 200 and outside.content == b"{}"

    attached_run_id, _attempt_id = running_attempt(ctx, project_id)
    attached = post_hook(
        client,
        token,
        session_start(str(repo), session_id="sess-2xx-attached"),
        **{"X-AgentTape-Run-Id": attached_run_id},
    )
    assert attached.status_code == 200 and attached.content == b"{}"


def test_refusals_use_the_standard_error_envelope(ctx: AppContext, client) -> None:
    """``client`` (from ``conftest``) is not loopback - used for the 403 case only."""
    _project_id, repo = svc_project(ctx)
    loopback = loopback_client(ctx)
    token = hooks_token(ctx)

    cases = [
        (
            400,
            loopback.post(
                "/api/v1/hooks/claude-code",
                content=b"not-json{",
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            ),
        ),
        (
            401,
            post_hook(
                loopback,
                "atc_wrong-not-real",
                session_start(str(repo), session_id="e"),
                drain=False,
            ),
        ),
        (
            403,
            client.post(
                "/api/v1/hooks/claude-code",
                json=session_start(str(repo), session_id="e2"),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            ),
        ),
        (
            415,
            loopback.post(
                "/api/v1/hooks/claude-code",
                json=session_start(str(repo), session_id="e3"),
                headers={"Content-Type": "text/plain", "Authorization": f"Bearer {token}"},
            ),
        ),
        (
            421,
            loopback.post(
                "/api/v1/hooks/claude-code",
                json=session_start(str(repo), session_id="e4"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                    "Host": "evil.example.com",
                },
            ),
        ),
        (
            422,
            post_hook(
                loopback, token, {"hook_event_name": "SessionStart", "cwd": str(repo)}, drain=False
            ),
        ),
    ]
    for expected_status, response in cases:
        assert response.status_code == expected_status, response.text
        body = response.json()
        assert set(body.keys()) == {"error"}
        assert set(body["error"].keys()) >= {"code", "message"}


def test_a_locked_database_never_slows_the_hooks_response(ctx: AppContext) -> None:
    if not ctx.settings.is_sqlite:
        pytest.skip("SQLite-only lock test")
    _project_id, repo = svc_project(ctx)
    token = hooks_token(ctx)
    file_path = str(repo / "src" / "greeter.py")
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}

    with live_server(ctx) as port:
        base = f"http://127.0.0.1:{port}"
        r0 = httpx.post(
            f"{base}/api/v1/hooks/claude-code",
            json=session_start(str(repo), session_id="sess-lock"),
            headers=headers,
            timeout=5,
        )
        assert r0.status_code == 200
        ingest = hooks_router.hook_ingest(ctx)
        assert ingest.drain(timeout=5)

        db_path = Path(str(ctx.settings.resolved_database_url).removeprefix("sqlite:///"))
        lock_started = threading.Event()
        answered = threading.Event()
        released = threading.Event()

        def hold_lock() -> None:
            # Held until the hook has answered. The bound only ends a broken test; it is
            # longer than the request timeout, so a hook that waited on the lock fails the
            # POST instead of slipping through after the lock is gone.
            conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=10)
            conn.execute("BEGIN EXCLUSIVE")
            lock_started.set()
            answered.wait(timeout=30)
            conn.rollback()
            conn.close()
            released.set()

        holder = threading.Thread(target=hold_lock)
        holder.start()
        assert lock_started.wait(timeout=5)

        try:
            r1 = httpx.post(
                f"{base}/api/v1/hooks/claude-code",
                json=post_read(str(repo), file_path, session_id="sess-lock"),
                headers=headers,
                timeout=5,
            )
            # The response arrived while the exclusive lock was still held.
            assert not released.is_set()
        finally:
            answered.set()
        assert r1.status_code == 200 and r1.json() == {}

        holder.join(timeout=10)
        assert released.is_set()
        assert ingest.drain(timeout=10)

        loopback = loopback_client(ctx)
        run_id = loopback.get("/api/v1/runs").json()["items"][0]["id"]
        events = loopback.get(f"/api/v1/runs/{run_id}/events").json()["items"]
        assert any(e["type"] == "tool_result" for e in events)


def test_saturation_returns_503_and_the_next_accepted_hook_records_the_gap(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)

    post_hook(client, token, session_start(str(repo), session_id="sess-drop"))

    ingest = hooks_router.hook_ingest(ctx)
    monkeypatch.setattr(hook_ingest_module, "MAX_PENDING_HOOKS", 1)
    release = threading.Event()
    started = threading.Event()

    def blocker() -> None:
        started.set()
        release.wait(timeout=10)

    try:
        # Occupy the single ingest thread without taking a pending slot, so the next hook
        # holds the one slot while it waits and the one after it is refused. (A hook sent
        # through ingest.submit would run to completion instead of holding the thread.)
        ingest._executor.submit(blocker)
        assert started.wait(timeout=5)

        occupying = post_hook(
            client,
            token,
            pre_read(str(repo), file_path=str(repo / "src" / "greeter.py"), session_id="sess-drop"),
            drain=False,
        )
        assert occupying.status_code == 200

        saturated = post_hook(
            client,
            token,
            post_read(
                str(repo), file_path=str(repo / "src" / "greeter.py"), session_id="sess-drop"
            ),
            drain=False,
        )
        assert saturated.status_code == 503
        body = saturated.json()
        assert set(body.keys()) == {"error"}

        release.set()
        assert ingest.drain(timeout=10)

        accepted = post_hook(
            client,
            token,
            pre_read(
                str(repo),
                file_path=str(repo / "src" / "greeter.py"),
                session_id="sess-drop",
                tool_use_id="toolu_after_drop",
            ),
        )
        assert accepted.status_code == 200
    finally:
        release.set()

    run_id = client.get("/api/v1/runs").json()["items"][0]["id"]
    with ctx.session() as session:
        row = session.scalars(select(ObservedSession).where(ObservedSession.run_id == run_id)).one()
        assert row.details.get("dropped_hook_events") == 1
        warnings = session.scalars(
            select(Event).where(Event.run_id == run_id, Event.severity == "warning")
        ).all()
        assert any("not recorded" in (w.summary or "").lower() for w in warnings)


def test_submit_releases_its_pending_slot_when_executor_submit_raises(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If ``ThreadPoolExecutor.submit`` itself raises (e.g. the executor
    is already shutting down), the pending slot must not leak - ``_run``'s own ``finally``
    never runs for a job that was never actually scheduled, so ``submit`` must release the
    slot itself before re-raising. With room for exactly one pending hook, the next hook is
    accepted only if that happened."""
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)
    monkeypatch.setattr(hook_ingest_module, "MAX_PENDING_HOOKS", 1)
    ingest = hooks_router.hook_ingest(ctx)
    hook = HookEvent(name="SessionStart", session_id="sess-submit-raises", cwd=str(repo))

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("cannot schedule new futures after shutdown")

    with monkeypatch.context() as patched:
        patched.setattr(ingest._executor, "submit", boom)
        with pytest.raises(RuntimeError):
            ingest.submit(hook)

    accepted = post_hook(client, token, session_start(str(repo), session_id="sess-after-raise"))
    assert accepted.status_code == 200


def test_exceeding_the_session_limit_returns_429_and_counts_as_dropped(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        hooks_router, "_SESSION_LIMITER", RateLimiter(rate=1, per_seconds=3600, burst=0)
    )
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)

    r = post_hook(client, token, session_start(str(repo), session_id="sess-flood"), drain=False)
    assert r.status_code == 429
    assert set(r.json().keys()) == {"error"}
    assert int(r.headers["Retry-After"]) >= 1

    ingest = hooks_router.hook_ingest(ctx)
    assert ingest.take_dropped(session_hash("sess-flood")) == 1


def test_the_21st_failed_token_from_one_client_gets_429(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hooks_router, "_AUTH_FAILURE_LIMITER", RateLimiter(rate=20, per_seconds=60))
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    body = session_start(str(repo), session_id="sess-auth-flood")

    responses = [
        post_hook(client, "atc_wrong-token-value-not-real", body, drain=False) for _ in range(21)
    ]
    assert [r.status_code for r in responses[:20]] == [401] * 20
    assert responses[20].status_code == 429
    assert int(responses[20].headers["Retry-After"]) >= 1


def test_content_length_over_the_cap_is_refused(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hooks_router, "MAX_HOOK_BODY_BYTES", 64)
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)
    body = session_start(str(repo), session_id="sess-oversized" * 20)

    r = post_hook(client, token, body, drain=False)
    assert r.status_code == 413


def test_a_chunked_body_over_the_cap_is_refused(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hooks_router, "MAX_HOOK_BODY_BYTES", 32)
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)
    payload = json.dumps(
        {"hook_event_name": "SessionStart", "session_id": "sess-chunked", "cwd": str(repo)}
    ).encode()

    def chunks():  # type: ignore[no-untyped-def]
        for i in range(0, len(payload), 8):
            yield payload[i : i + 8]

    r = client.post(
        "/api/v1/hooks/claude-code",
        content=chunks(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 413


def test_secret_shaped_values_in_a_bash_command_and_its_stdout_are_redacted(
    ctx: AppContext,
) -> None:
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)
    leak = "AKIA" + "QQQQWWWWEEEERRRR"

    post_hook(client, token, session_start(str(repo), session_id="sess-secret"))
    post_hook(
        client,
        token,
        post_tool_use(
            str(repo),
            tool_name="Bash",
            tool_use_id="toolu_leak",
            tool_input={"command": f"echo {leak}"},
            tool_response={"stdout": f"{leak}\n", "stderr": "", "interrupted": False},
            session_id="sess-secret",
        ),
    )

    run_id = client.get("/api/v1/runs").json()["items"][0]["id"]
    events = client.get(f"/api/v1/runs/{run_id}/events").json()["items"]
    assert leak not in json.dumps(events)


def test_observed_run_creation_is_bounded_per_machine(ctx: AppContext) -> None:
    """A hooks-token holder minting a fresh session_id per SessionStart creates at
    most MAX_NEW_OBSERVED_SESSIONS_PER_MINUTE observed runs per minute; every response is
    still exactly 200 {}, and the refused session gets no observed_sessions row at all."""
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)
    limit = observed_sessions.MAX_NEW_OBSERVED_SESSIONS_PER_MINUTE

    for i in range(limit + 1):
        r = post_hook(client, token, session_start(str(repo), session_id=f"sess-flood-{i}"))
        assert r.status_code == 200 and r.json() == {}

    runs = client.get("/api/v1/runs").json()["items"]
    assert len(runs) == limit

    with ctx.session() as session:
        rows = session.scalars(select(ObservedSession)).all()
        assert len(rows) == limit
        hashes = {row.session_hash for row in rows}
        assert session_hash(f"sess-flood-{limit}") not in hashes

    # A hook for an already-correlated session is never affected by the creation limiter.
    with ctx.session() as session:
        row0 = session.scalars(
            select(ObservedSession).where(
                ObservedSession.session_hash == session_hash("sess-flood-0")
            )
        ).one()
        run0_id = row0.run_id

    file_path = str(repo / "src" / "greeter.py")
    r = post_hook(client, token, pre_read(str(repo), file_path, session_id="sess-flood-0"))
    assert r.status_code == 200 and r.json() == {}
    events = client.get(f"/api/v1/runs/{run0_id}/events").json()["items"]
    assert any(e["type"] == "tool_call" for e in events)

    # The refused hook is not forgotten. Once the limiter has room again, the refused
    # session's next hook creates its run, and that run says its evidence window is
    # incomplete: the refused SessionStart counts as dropped.
    observed_sessions._creation_limiter(ctx).reset()
    refused = f"sess-flood-{limit}"
    r = post_hook(client, token, pre_read(str(repo), file_path, session_id=refused))
    assert r.status_code == 200 and r.json() == {}
    with ctx.session() as session:
        row = session.scalars(
            select(ObservedSession).where(ObservedSession.session_hash == session_hash(refused))
        ).one()
        refused_run_id = row.run_id
        assert row.details.get("dropped_hook_events") == 1
    events = client.get(f"/api/v1/runs/{refused_run_id}/events").json()["items"]
    warnings = [e for e in events if (e.get("payload") or {}).get("phase") == "hooks_dropped"]
    assert len(warnings) == 1
    assert warnings[0]["severity"] == "warning"
    assert warnings[0]["payload"]["dropped"] == 1


def test_dropped_hook_counts_are_bounded_per_process(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session refused by the creation limiter keeps its dropped count until its
    run exists, and a flood of fresh session ids may never create one; the per-session
    counts are bounded (oldest session first) so the flood cannot grow memory. An evicted
    count is not forgotten: it becomes an unattributed drop."""
    monkeypatch.setattr(ingest_state, "MAX_TRACKED_DROPPED_SESSIONS", 3)
    ingest = hooks_router.hook_ingest(ctx)
    for i in range(5):
        ingest.mark_dropped(f"sha256:{i}")
    assert ingest.take_dropped("sha256:0") == 0
    assert ingest.take_dropped("sha256:1") == 0
    assert [ingest.take_dropped(f"sha256:{i}") for i in (2, 3, 4)] == [1, 1, 1]
    assert ingest.drops.unattributed() == 2


def test_post_hook_fails_the_test_when_ingest_does_not_drain(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A negative assertion after ``post_hook`` ("nothing was recorded") must not
    pass only because the hook was still queued; a drain that times out fails loudly."""
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)
    monkeypatch.setattr(hook_ingest_module.HookIngest, "drain", lambda self, timeout=10.0: False)
    with pytest.raises(AssertionError, match="did not drain"):
        post_hook(client, token, session_start(str(repo), session_id="sess-undrained"))


def test_malformed_known_events_are_refused_and_record_nothing(ctx: AppContext) -> None:
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)

    missing_session_id = {"hook_event_name": "SessionStart", "cwd": str(repo)}
    r1 = post_hook(client, token, missing_session_id, drain=False)
    assert r1.status_code == 422

    relative_cwd = {
        "hook_event_name": "SessionStart",
        "session_id": "sess-bad-cwd",
        "cwd": "relative/path",
    }
    r2 = post_hook(client, token, relative_cwd, drain=False)
    assert r2.status_code == 422

    non_dict_tool_input = {
        "hook_event_name": "PreToolUse",
        "session_id": "sess-bad-input",
        "cwd": str(repo),
        "tool_name": "Read",
        "tool_use_id": "toolu_bad",
        "tool_input": "not-a-dict",
    }
    r3 = post_hook(client, token, non_dict_tool_input, drain=False)
    assert r3.status_code == 422

    nul_cwd = {
        "hook_event_name": "SessionStart",
        "session_id": "sess-nul-cwd",
        "cwd": f"{repo}/../../../../etc\x00",
    }
    r4 = post_hook(client, token, nul_cwd, drain=False)
    assert r4.status_code == 422

    assert client.get("/api/v1/runs").json()["items"] == []
    with ctx.session() as session:
        assert session.scalars(select(ObservedSession)).first() is None


def test_capture_helpers_do_not_shadow_the_repo_project_fixture() -> None:
    """``conftest.repo_project`` is a fixture (the ``findings-repo`` project id); a
    capture helper of the same name with a different shape shadowed it in every module
    that imported it."""
    assert not hasattr(capture_helpers, "repo_project")
    assert callable(capture_helpers.svc_project)


def test_drain_waits_for_a_queued_interactive_finding_too(ctx: AppContext) -> None:
    """An ``in_order`` job (an interactive MCP finding) is pending work like a
    hook, so ``drain()`` never reports an empty queue while one is still waiting."""
    ingest = hook_ingest_module.hook_ingest(ctx)
    gate = threading.Event()
    held = ingest.in_order(gate.wait)
    try:
        assert ingest.drain(timeout=0.05) is False
    finally:
        gate.set()
    held.result(timeout=10)
    assert ingest.drain(timeout=10)


def test_a_cancelled_in_order_job_releases_its_pending_slot(ctx: AppContext) -> None:
    ingest = hook_ingest_module.hook_ingest(ctx)
    gate = threading.Event()
    held = ingest.in_order(gate.wait)
    queued = ingest.in_order(lambda: None)
    assert queued.cancel()
    gate.set()
    held.result(timeout=10)
    assert ingest.drain(timeout=10)


def test_resolve_for_mcp_refuses_to_run_off_the_ingest_thread(ctx: AppContext) -> None:
    """The ingest-thread contract is asserted, not just documented."""
    _project_id, repo = svc_project(ctx)
    with ctx.session() as session, pytest.raises(AssertionError, match="ingest thread"):
        observed_sessions.ObservedSessionService(ctx).resolve_for_mcp(
            session, cwd=str(repo), instance_id="inst-off-thread", session_id=None
        )
    ingest = hook_ingest_module.hook_ingest(ctx)
    assert ingest.in_order(ingest_state.on_ingest_thread).result(timeout=10) is True
    assert ingest_state.on_ingest_thread() is False


# ------------------------------------ hooks refused or abandoned after authentication


def test_the_hooks_token_is_verified_without_waiting_on_the_shared_thread_pool(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing before ``submit`` waits on the default executor: a saturated pool (other
    work blocked on the database) could otherwise hold a hook past Claude Code's 3s
    timeout before its body is even read."""
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)
    real = CaptureTokenStore.verify
    where: list[str] = []

    def spy(self: CaptureTokenStore, name: str, value: str | None) -> bool:
        try:
            asyncio.get_running_loop()
            where.append("event loop")
        except RuntimeError:
            where.append("worker thread")
        return real(self, name, value)

    monkeypatch.setattr(CaptureTokenStore, "verify", spy)
    r = post_hook(client, token, session_start(str(repo), session_id="sess-sync-token"))
    assert r.status_code == 200
    assert where == ["event loop"]


def _open_session(ctx: AppContext, session_id: str) -> tuple[str, str]:
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)
    assert (
        post_hook(client, token, session_start(str(repo), session_id=session_id)).status_code == 200
    )
    with ctx.session() as session:
        row = session.scalars(
            select(ObservedSession).where(ObservedSession.session_hash == session_hash(session_id))
        ).one()
        return row.run_id, token


def _dropped_on(ctx: AppContext, run_id: str) -> int:
    hook_ingest_module.hook_ingest(ctx).in_order(lambda: None).result(timeout=10)
    with ctx.session() as session:
        row = session.scalars(select(ObservedSession).where(ObservedSession.run_id == run_id)).one()
        return int(row.details.get("dropped_hook_events") or 0)


def test_a_hook_abandoned_before_its_body_was_read_counts_as_dropped(ctx: AppContext) -> None:
    """Claude Code gave up (3s) and closed the connection before AgentTape read the body:
    nothing names the session, so the drop is written onto every open session."""
    run_id, token = _open_session(ctx, "sess-abandoned")
    app = create_app(ctx, web_dist=ctx.settings.data_dir / "no-web-dist")
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/hooks/claude-code",
        "raw_path": b"/api/v1/hooks/claude-code",
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"host", b"127.0.0.1"),
            (b"content-type", b"application/json"),
            (b"authorization", f"Bearer {token}".encode()),
            (b"content-length", b"120"),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 80),
    }
    asyncio.run(app(scope, receive, send))
    assert _dropped_on(ctx, run_id) == 1


def test_an_authenticated_hook_over_the_body_cap_counts_as_dropped(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, token = _open_session(ctx, "sess-over-cap")
    monkeypatch.setattr(hooks_router, "MAX_HOOK_BODY_BYTES", 64)
    client = loopback_client(ctx)
    body = pre_read("/tmp", "/tmp/x" * 40, session_id="sess-over-cap")
    assert post_hook(client, token, body, drain=False).status_code == 413
    assert _dropped_on(ctx, run_id) == 1


def test_an_authenticated_hook_that_is_not_json_counts_as_dropped(ctx: AppContext) -> None:
    run_id, token = _open_session(ctx, "sess-bad-json")
    client = loopback_client(ctx)
    r = client.post(
        "/api/v1/hooks/claude-code",
        content=b"{not json",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400
    assert _dropped_on(ctx, run_id) == 1


def test_a_malformed_hook_naming_its_session_counts_as_dropped_for_that_session(
    ctx: AppContext,
) -> None:
    run_id, token = _open_session(ctx, "sess-malformed")
    other_run_id, _ = _open_session_in_same_project(ctx, "sess-bystander", token)
    client = loopback_client(ctx)
    malformed = {"hook_event_name": "PreToolUse", "session_id": "sess-malformed", "cwd": "rel"}
    assert post_hook(client, token, malformed, drain=False).status_code == 422
    assert _dropped_on(ctx, run_id) == 0  # pending for its own session, not every session
    assert hook_ingest_module.hook_ingest(ctx).take_dropped(session_hash("sess-malformed")) == 1
    assert _dropped_on(ctx, other_run_id) == 0


def test_a_rejected_token_is_not_counted_as_a_drop(ctx: AppContext) -> None:
    """Only an authenticated hook counts: without the token anyone could mark sessions."""
    run_id, _token = _open_session(ctx, "sess-wrong-token")
    client = loopback_client(ctx)
    body = pre_read("/tmp", "/tmp/x", session_id="sess-wrong-token")
    assert post_hook(client, "atc_wrong", body, drain=False).status_code == 401
    assert _dropped_on(ctx, run_id) == 0


def _open_session_in_same_project(ctx: AppContext, session_id: str, token: str) -> tuple[str, str]:
    client = loopback_client(ctx)
    repo = Path(ctx.settings.allowed_roots[0]) / "svc"
    assert (
        post_hook(client, token, session_start(str(repo), session_id=session_id)).status_code == 200
    )
    with ctx.session() as session:
        row = session.scalars(
            select(ObservedSession).where(ObservedSession.session_hash == session_hash(session_id))
        ).one()
        return row.run_id, token
