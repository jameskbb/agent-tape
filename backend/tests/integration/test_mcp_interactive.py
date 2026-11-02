"""Interactive and Herdr-hosted MCP capture: ``record_finding`` from a session AgentTape did
not launch resolves to a run through the one ``ObservedSessionService.resolve_for_mcp``
resolver, authenticated by the per-machine ``mcp`` token."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator, Mapping

import pytest
from fastapi.testclient import TestClient
from mcp import Client, StdioServerParameters
from sqlalchemy import select

from agenttape.api.routers import mcp as mcp_router
from agenttape.application.context import AppContext
from agenttape.application.schemas.capture import FindingRecordIn
from agenttape.application.services.observed_sessions import (
    MCP_BINDING_LEASE_SECONDS,
    ObservedSessionService,
)
from agenttape.bootstrap import build_context
from agenttape.capture.claude_hooks import session_hash
from agenttape.config import Settings
from agenttape.infrastructure.clock import ManualClock
from agenttape.infrastructure.db.extra_models.observed_sessions import ObservedSession
from agenttape.infrastructure.db.models import Event, Run
from agenttape.mcp.client import CaptureClient, CaptureClientConfig, CaptureError
from agenttape.mcp.server import build_server
from agenttape.security.capture import CaptureTokenStore, mint_attempt_token
from tests.integration.capture_helpers import (
    hooks_token,
    hosted_attempt,
    interactive_client,
    live_server,
    loopback_client,
    mcp_token,
    post_hook,
    running_attempt,
    seed_hook_session,
    svc_project,
)


def finding_events_for_run(ctx: AppContext, run_id: str) -> list[Event]:
    with ctx.session() as session:
        return list(
            session.scalars(
                select(Event).where(Event.run_id == run_id, Event.type == "finding")
            ).all()
        )


def observed_row_for_run(ctx: AppContext, run_id: str) -> tuple[str | None, str, str]:
    """``(mcp_instance, mode, source)`` of the ``observed_sessions`` row for ``run_id``."""
    with ctx.session() as session:
        row = session.scalars(select(ObservedSession).where(ObservedSession.run_id == run_id)).one()
        return row.mcp_instance, row.mode, row.source


def run_kind_and_source(ctx: AppContext, run_id: str) -> tuple[str, str]:
    with ctx.session() as session:
        run = session.get(Run, run_id)
        assert run is not None
        return run.kind, run.source


def post_finding(
    client: TestClient,
    token: str,
    *,
    cwd: str,
    instance_id: str,
    session_id: str | None = None,
    text: str = "x",
    target: dict[str, object] | None = None,
):  # type: ignore[no-untyped-def]
    """Raw ``POST /api/v1/mcp/findings`` (not the MCP tool client) so a test can assert
    the exact status code and ``error.code``."""
    body: dict[str, object] = {
        "kind": "risk",
        "text": text,
        "citations": [],
        "cwd": cwd,
        "instance_id": instance_id,
    }
    if session_id is not None:
        body["session_id"] = session_id
    if target is not None:
        body["target"] = target
    return client.post(
        "/api/v1/mcp/findings",
        json=body,
        headers={"content-type": "application/json", "authorization": f"Bearer {token}"},
    )


@pytest.mark.asyncio
async def test_hooks_observed_session_gets_the_finding_and_binds_the_instance(
    ctx: AppContext,
) -> None:
    _project_id, repo = svc_project(ctx)
    hooks_tok = hooks_token(ctx)
    mcp_tok = mcp_token(ctx)

    with loopback_client(ctx) as http_client:
        seed_hook_session(http_client, hooks_tok, session_id="claude-sess-1", cwd=str(repo))

        capture_client = interactive_client(
            http_client, token=mcp_tok, cwd=str(repo), instance_id="instance-a"
        )
        async with Client(build_server(capture_client)) as mcp_client:
            result = await mcp_client.call_tool(
                "record_finding",
                {
                    "kind": "risk",
                    "text": "greet() ignores its caller",
                    "citations": [{"path": "src/greeter.py", "quote": "def greet():"}],
                },
            )
            assert result.is_error is False
            run_id = (result.structured_content or {}).get("run_id")

        response = http_client.get(f"/api/v1/runs/{run_id}/context/findings")
        assert response.status_code == 200
        findings = response.json()
        assert len(findings) == 1
        assert findings[0]["source"] == "agenttape-mcp"

    events = finding_events_for_run(ctx, run_id)
    assert len(events) == 1
    assert events[0].source == "agenttape-mcp"
    mcp_instance, mode, source = observed_row_for_run(ctx, run_id)
    assert mcp_instance == "instance-a"
    assert mode == "observed"
    assert source == "hook"
    with ctx.session() as session:
        row = session.scalars(select(ObservedSession).where(ObservedSession.run_id == run_id)).one()
        assert row.details.get("mcp_resolved_by") == "cwd_sole_candidate"


@pytest.mark.asyncio
async def test_hosted_herdr_session_gets_the_finding_on_the_herdr_attempt(
    ctx: AppContext,
) -> None:
    project_id, repo = svc_project(ctx)
    herdr_run_id, herdr_attempt_id = hosted_attempt(ctx, project_id)
    hooks_tok = hooks_token(ctx)
    mcp_tok = mcp_token(ctx)

    with loopback_client(ctx) as http_client:
        seed_hook_session(http_client, hooks_tok, session_id="herdr-sess-1", cwd=str(repo))

        capture_client = interactive_client(
            http_client, token=mcp_tok, cwd=str(repo), instance_id="instance-herdr"
        )
        async with Client(build_server(capture_client)) as mcp_client:
            result = await mcp_client.call_tool(
                "record_finding",
                {"kind": "risk", "text": "hosted finding", "citations": []},
            )
            assert result.is_error is False
            run_id = (result.structured_content or {}).get("run_id")

    assert run_id == herdr_run_id
    events = finding_events_for_run(ctx, herdr_run_id)
    assert len(events) == 1
    assert events[0].attempt_id == herdr_attempt_id


@pytest.mark.asyncio
async def test_no_hooks_creates_an_observed_run_and_a_second_call_reuses_it(
    ctx: AppContext,
) -> None:
    _project_id, repo = svc_project(ctx)
    mcp_tok = mcp_token(ctx)

    with loopback_client(ctx) as http_client:
        capture_client = interactive_client(
            http_client, token=mcp_tok, cwd=str(repo), instance_id="instance-solo"
        )
        async with Client(build_server(capture_client)) as mcp_client:
            first = await mcp_client.call_tool(
                "record_finding", {"kind": "risk", "text": "first", "citations": []}
            )
            assert first.is_error is False
            run_id_1 = (first.structured_content or {}).get("run_id")

            second = await mcp_client.call_tool(
                "record_finding", {"kind": "note", "text": "second", "citations": []}
            )
            assert second.is_error is False
            run_id_2 = (second.structured_content or {}).get("run_id")

    assert run_id_1 == run_id_2
    kind, source = run_kind_and_source(ctx, run_id_1)
    assert kind == "observed"
    assert source == "mcp"
    assert len(finding_events_for_run(ctx, run_id_1)) == 2


@pytest.mark.asyncio
async def test_a_different_instance_in_the_same_directory_gets_its_own_run(
    ctx: AppContext,
) -> None:
    _project_id, repo = svc_project(ctx)
    mcp_tok = mcp_token(ctx)

    with loopback_client(ctx) as http_client:
        first_client = interactive_client(
            http_client, token=mcp_tok, cwd=str(repo), instance_id="instance-one"
        )
        async with Client(build_server(first_client)) as mcp_client:
            first = await mcp_client.call_tool(
                "record_finding", {"kind": "risk", "text": "first", "citations": []}
            )
            run_id_1 = (first.structured_content or {}).get("run_id")

        second_client = interactive_client(
            http_client, token=mcp_tok, cwd=str(repo), instance_id="instance-two"
        )
        async with Client(build_server(second_client)) as mcp_client:
            second = await mcp_client.call_tool(
                "record_finding", {"kind": "risk", "text": "second", "citations": []}
            )
            run_id_2 = (second.structured_content or {}).get("run_id")

    assert run_id_1 != run_id_2


@pytest.mark.asyncio
async def test_two_hook_sessions_ambiguous_without_session_id(ctx: AppContext) -> None:
    _project_id, repo = svc_project(ctx)
    hooks_tok = hooks_token(ctx)
    mcp_tok = mcp_token(ctx)

    with loopback_client(ctx) as http_client:
        seed_hook_session(http_client, hooks_tok, session_id="claude-sess-a", cwd=str(repo))
        seed_hook_session(http_client, hooks_tok, session_id="claude-sess-b", cwd=str(repo))

        capture_client = interactive_client(
            http_client, token=mcp_tok, cwd=str(repo), instance_id="instance-ambiguous"
        )
        async with Client(build_server(capture_client)) as mcp_client:
            result = await mcp_client.call_tool(
                "record_finding", {"kind": "risk", "text": "which one", "citations": []}
            )
            assert result.is_error is True
            text = " ".join(getattr(c, "text", "") for c in result.content)
            assert "session_id" in text


@pytest.mark.asyncio
async def test_session_id_resolves_the_named_session_among_two_hook_sessions(
    ctx: AppContext,
) -> None:
    _project_id, repo = svc_project(ctx)
    hooks_tok = hooks_token(ctx)
    mcp_tok = mcp_token(ctx)

    with loopback_client(ctx) as http_client:
        seed_hook_session(http_client, hooks_tok, session_id="claude-sess-a", cwd=str(repo))
        seed_hook_session(http_client, hooks_tok, session_id="claude-sess-b", cwd=str(repo))

        capture_client = interactive_client(
            http_client, token=mcp_tok, cwd=str(repo), instance_id="instance-named"
        )
        async with Client(build_server(capture_client)) as mcp_client:
            result = await mcp_client.call_tool(
                "record_finding",
                {
                    "kind": "risk",
                    "text": "named session",
                    "citations": [],
                    "session_id": "claude-sess-b",
                },
            )
            assert result.is_error is False
            run_id = (result.structured_content or {}).get("run_id")

    with ctx.session() as session:
        row = session.scalars(
            select(ObservedSession).where(
                ObservedSession.session_hash == session_hash("claude-sess-b")
            )
        ).one()
        assert row.run_id == run_id
        assert row.details.get("mcp_resolved_by") == "session_id"


def test_session_id_of_a_session_in_another_directory_is_refused(ctx: AppContext) -> None:
    """The shared per-machine ``mcp`` token must not let a caller in one directory
    name another session's id and write into that session's run, nor steal its binding."""
    _project_id, repo = svc_project(ctx)
    hooks_tok = hooks_token(ctx)
    mcp_tok = mcp_token(ctx)
    outside = str(ctx.settings.data_dir.parent / "outside-project")

    with loopback_client(ctx) as client:
        seed_hook_session(client, hooks_tok, session_id="victim", cwd=str(repo))

        response = post_finding(
            client, mcp_tok, cwd=outside, instance_id="attacker", session_id="victim"
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "session_mismatch"

        with ctx.session() as session:
            row = session.scalars(
                select(ObservedSession).where(
                    ObservedSession.session_hash == session_hash("victim")
                )
            ).one()
            victim_run_id = row.run_id
            assert row.mcp_instance is None

    assert finding_events_for_run(ctx, victim_run_id) == []


@pytest.fixture
def mctx(settings: Settings) -> Iterator[AppContext]:
    """A context on a :class:`ManualClock`, so a test can let an MCP binding's lease lapse
    without sleeping."""
    context = build_context(settings, clock=ManualClock(), migrate=True)
    yield context
    context.close()


def _row_for_session(ctx: AppContext, session_id: str) -> ObservedSession:
    with ctx.session() as session:
        row = session.scalars(
            select(ObservedSession).where(ObservedSession.session_hash == session_hash(session_id))
        ).one()
        session.expunge(row)
        return row


def test_session_id_of_a_session_bound_to_another_instance_is_refused_while_its_lease_is_live(
    mctx: AppContext,
) -> None:
    """A binding is a lease renewed by every call from the bound MCP server;
    while that server has called within ``MCP_BINDING_LEASE_SECONDS``, a second caller
    naming the same session id cannot take the row over, even from the same directory."""
    clock = mctx.clock
    assert isinstance(clock, ManualClock)
    _project_id, repo = svc_project(mctx)
    hooks_tok = hooks_token(mctx)
    mcp_tok = mcp_token(mctx)
    lease = MCP_BINDING_LEASE_SECONDS

    with loopback_client(mctx) as client:
        seed_hook_session(client, hooks_tok, session_id="victim", cwd=str(repo))

        first = post_finding(
            client, mcp_tok, cwd=str(repo), instance_id="victim-instance", session_id="victim"
        )
        assert first.status_code == 201
        victim_run_id = first.json()["run_id"]

        clock.advance(lease - 1)
        second = post_finding(
            client, mcp_tok, cwd=str(repo), instance_id="attacker-instance", session_id="victim"
        )
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "session_mismatch"

        # The bound server calls again, renewing its lease; the attacker is still refused
        # once the original lease would have lapsed.
        renew = post_finding(client, mcp_tok, cwd=str(repo), instance_id="victim-instance")
        assert renew.status_code == 201
        assert renew.json()["run_id"] == victim_run_id
        clock.advance(lease - 1)
        third = post_finding(
            client, mcp_tok, cwd=str(repo), instance_id="attacker-instance", session_id="victim"
        )
        assert third.status_code == 409
        assert third.json()["error"]["code"] == "session_mismatch"

        row = _row_for_session(mctx, "victim")
        assert row.mcp_instance == "victim-instance"
        assert "mcp_rebinds" not in row.details

    assert len(finding_events_for_run(mctx, victim_run_id)) == 2


def test_a_restarted_mcp_server_takes_its_session_over_once_the_old_lease_lapses(
    mctx: AppContext,
) -> None:
    """``instance_id`` identifies an MCP server process, so a restarted server
    (a ``/mcp`` reconnect, a crash) arrives with a new one. Once the old instance has been
    quiet for longer than ``MCP_BINDING_LEASE_SECONDS``, the session's own new server can
    take the row over, by ``session_id`` or as the sole compatible candidate, and the
    handover is recorded on the row."""
    clock = mctx.clock
    assert isinstance(clock, ManualClock)
    _project_id, repo = svc_project(mctx)
    hooks_tok = hooks_token(mctx)
    mcp_tok = mcp_token(mctx)

    with loopback_client(mctx) as client:
        seed_hook_session(client, hooks_tok, session_id="sess-b", cwd=str(repo))
        first = post_finding(
            client, mcp_tok, cwd=str(repo), instance_id="instance-1", session_id="sess-b"
        )
        assert first.status_code == 201
        run_id = first.json()["run_id"]

        clock.advance(MCP_BINDING_LEASE_SECONDS + 1)
        second = post_finding(
            client, mcp_tok, cwd=str(repo), instance_id="instance-2", session_id="sess-b"
        )
        assert second.status_code == 201
        assert second.json()["run_id"] == run_id
        row = _row_for_session(mctx, "sess-b")
        assert row.mcp_instance == "instance-2"
        assert [r["from"] for r in row.details["mcp_rebinds"]] == ["instance-1"]

        # The old instance now holds no lease: a call from it is refused while
        # instance-2's lease is live.
        stale = post_finding(
            client, mcp_tok, cwd=str(repo), instance_id="instance-1", session_id="sess-b"
        )
        assert stale.status_code == 409

        # Without a session_id, a lapsed row is again the sole compatible candidate.
        clock.advance(MCP_BINDING_LEASE_SECONDS + 1)
        third = post_finding(client, mcp_tok, cwd=str(repo), instance_id="instance-3")
        assert third.status_code == 201
        assert third.json()["run_id"] == run_id
        row = _row_for_session(mctx, "sess-b")
        assert row.mcp_instance == "instance-3"
        assert [r["from"] for r in row.details["mcp_rebinds"]] == ["instance-1", "instance-2"]

    assert len(finding_events_for_run(mctx, run_id)) == 3


def test_session_id_naming_an_attached_session_is_refused(ctx: AppContext) -> None:
    """A launched run's attached row is never resolved through the per-machine
    ``mcp`` token, even when the caller's cwd matches the attempt's own workdir - only
    the per-attempt token (X-AgentTape-Attempt) may write to it."""
    project_id, repo = svc_project(ctx)
    hooks_tok = hooks_token(ctx)
    mcp_tok = mcp_token(ctx)
    attached_run_id, _attached_attempt_id = running_attempt(ctx, project_id)

    with loopback_client(ctx) as client:
        r = post_hook(
            client,
            hooks_tok,
            {
                "hook_event_name": "SessionStart",
                "session_id": "attached-session",
                "cwd": str(repo),
                "source": "startup",
            },
            **{"X-AgentTape-Run-Id": attached_run_id},
        )
        assert r.status_code == 200

        response = post_finding(
            client,
            mcp_tok,
            cwd=str(repo),
            instance_id="interactive-instance",
            session_id="attached-session",
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "session_mismatch"

    assert finding_events_for_run(ctx, attached_run_id) == []


def test_a_review_target_cannot_reach_a_launched_run_through_the_mcp_token(
    ctx: AppContext,
) -> None:
    """A review-shaped target (run_id/finding_key/outcome) posted through the
    interactive path onto an attached row is refused exactly like any other finding - a
    forged review outcome can never land on a launched run through the mcp token."""
    project_id, repo = svc_project(ctx)
    hooks_tok = hooks_token(ctx)
    mcp_tok = mcp_token(ctx)
    attached_run_id, _attached_attempt_id = running_attempt(ctx, project_id)

    with loopback_client(ctx) as client:
        r = post_hook(
            client,
            hooks_tok,
            {
                "hook_event_name": "SessionStart",
                "session_id": "attached-review-session",
                "cwd": str(repo),
                "source": "startup",
            },
            **{"X-AgentTape-Run-Id": attached_run_id},
        )
        assert r.status_code == 200

        response = post_finding(
            client,
            mcp_tok,
            cwd=str(repo),
            instance_id="interactive-instance",
            session_id="attached-review-session",
            target={
                "run_id": attached_run_id,
                "finding_key": "some-finding-key",
                "outcome": "refuted",
            },
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "session_mismatch"

    assert finding_events_for_run(ctx, attached_run_id) == []


def test_session_id_with_an_out_of_project_cwd_is_refused(ctx: AppContext) -> None:
    """An out-of-project cwd is refused on the session_id path exactly as it is
    on every other path."""
    _project_id, repo = svc_project(ctx)
    hooks_tok = hooks_token(ctx)
    mcp_tok = mcp_token(ctx)

    with loopback_client(ctx) as client:
        seed_hook_session(client, hooks_tok, session_id="victim", cwd=str(repo))

        response = post_finding(
            client,
            mcp_tok,
            cwd="/nowhere/outside/any/project",
            instance_id="attacker",
            session_id="victim",
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "session_mismatch"

        runs = client.get("/api/v1/runs").json()["items"]
        assert len(runs) == 1


@pytest.mark.asyncio
async def test_cwd_outside_every_project_is_refused(ctx: AppContext) -> None:
    _project_id, _repo = svc_project(ctx)
    mcp_tok = mcp_token(ctx)
    outside = str(ctx.settings.data_dir.parent / "outside-project")

    with loopback_client(ctx) as http_client:
        capture_client = interactive_client(
            http_client, token=mcp_tok, cwd=outside, instance_id="instance-outside"
        )
        async with Client(build_server(capture_client)) as mcp_client:
            result = await mcp_client.call_tool(
                "record_finding", {"kind": "risk", "text": "nope", "citations": []}
            )
            assert result.is_error is True
            text = " ".join(getattr(c, "text", "") for c in result.content)
            assert "registered AgentTape project" in text


@pytest.mark.asyncio
async def test_a_cancelled_sessions_session_id_is_refused(ctx: AppContext) -> None:
    _project_id, repo = svc_project(ctx)
    hooks_tok = hooks_token(ctx)
    mcp_tok = mcp_token(ctx)

    with loopback_client(ctx) as http_client:
        seed_hook_session(http_client, hooks_tok, session_id="claude-cancel-me", cwd=str(repo))

    with ctx.session() as session:
        row = session.scalars(
            select(ObservedSession).where(
                ObservedSession.session_hash == session_hash("claude-cancel-me")
            )
        ).one()
        run_id = row.run_id
        ObservedSessionService.close_rows_for_run(
            session, run_id, reason="cancelled", now=ctx.clock.now()
        )

    with loopback_client(ctx) as http_client:
        capture_client = interactive_client(
            http_client, token=mcp_tok, cwd=str(repo), instance_id="instance-cancelled"
        )
        async with Client(build_server(capture_client)) as mcp_client:
            result = await mcp_client.call_tool(
                "record_finding",
                {
                    "kind": "risk",
                    "text": "too late",
                    "citations": [],
                    "session_id": "claude-cancel-me",
                },
            )
            assert result.is_error is True
            text = " ".join(getattr(c, "text", "") for c in result.content)
            assert "stopped observing" in text

    assert finding_events_for_run(ctx, run_id) == []


def test_distinct_instance_ids_are_rate_limited_and_do_not_mint_n_runs(
    ctx: AppContext,
) -> None:
    """The interactive write limiter and ``resolve_for_mcp``'s "already
    bound" fast path must not be keyed purely on the caller-supplied ``instance_id`` - a
    caller holding the shared per-machine ``mcp`` token could otherwise mint a fresh
    ``instance_id`` on every call to dodge ``_WRITES`` entirely and create one new
    ``observed`` run per call. Once the limiter is keyed on the (loopback) client instead,
    calls past its capacity are refused (429) before a run is ever created, so many more
    calls than the limiter's capacity never mint that many runs."""
    _project_id, repo = svc_project(ctx)
    mcp_tok = mcp_token(ctx)
    n_calls = int(mcp_router._WRITES.capacity) + 10

    run_ids: set[str] = set()
    rate_limited = 0
    with loopback_client(ctx) as client:
        for i in range(n_calls):
            response = client.post(
                "/api/v1/mcp/findings",
                json={
                    "kind": "risk",
                    "text": f"finding {i}",
                    "citations": [],
                    "cwd": str(repo),
                    "instance_id": f"instance-{i}",
                },
                headers={
                    "content-type": "application/json",
                    "authorization": f"Bearer {mcp_tok}",
                },
            )
            if response.status_code == 201:
                run_ids.add(response.json()["run_id"])
            elif response.status_code == 429:
                rate_limited += 1
            else:
                raise AssertionError(f"unexpected status {response.status_code}: {response.text}")

    assert rate_limited > 0, "distinct instance_ids should not bypass the write limiter"
    assert len(run_ids) < n_calls, "distinct instance_ids should not each mint their own run"


def test_interactive_auth_rejects_every_wrong_token_variant(ctx: AppContext) -> None:
    project_id, repo = svc_project(ctx)
    _run_id, attempt_id = running_attempt(ctx, project_id)
    attempt_tok = mint_attempt_token(ctx.settings.data_dir, attempt_id)
    hooks_tok = hooks_token(ctx)
    mcp_tok = mcp_token(ctx)

    body = {
        "kind": "risk",
        "text": "x",
        "citations": [],
        "cwd": str(repo),
        "instance_id": "instance-auth",
    }

    with loopback_client(ctx) as client:
        r_missing = client.post(
            "/api/v1/mcp/findings", json=body, headers={"content-type": "application/json"}
        )
        r_wrong = client.post(
            "/api/v1/mcp/findings",
            json=body,
            headers={"content-type": "application/json", "authorization": "Bearer atc_wrong"},
        )
        r_attempt_token = client.post(
            "/api/v1/mcp/findings",
            json=body,
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {attempt_tok}",
            },
        )
        r_hooks_token = client.post(
            "/api/v1/mcp/findings",
            json=body,
            headers={"content-type": "application/json", "authorization": f"Bearer {hooks_tok}"},
        )
        for r in (r_missing, r_wrong, r_attempt_token, r_hooks_token):
            assert r.status_code == 401
            assert r.json()["error"]["code"] == "invalid_capture_token"

        r_mcp_on_hooks = post_hook(
            client,
            mcp_tok,
            {"hook_event_name": "SessionStart", "session_id": "x", "cwd": str(repo)},
        )
        assert r_mcp_on_hooks.status_code == 401


def test_interactive_requests_without_absolute_cwd_or_instance_id_get_422(
    ctx: AppContext,
) -> None:
    _project_id, repo = svc_project(ctx)
    mcp_tok = mcp_token(ctx)

    with loopback_client(ctx) as client:
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {mcp_tok}",
        }
        r_relative_cwd = client.post(
            "/api/v1/mcp/findings",
            json={
                "kind": "risk",
                "text": "x",
                "citations": [],
                "cwd": "relative/path",
                "instance_id": "instance-422",
            },
            headers=headers,
        )
        assert r_relative_cwd.status_code == 422

        r_missing_instance = client.post(
            "/api/v1/mcp/findings",
            json={"kind": "risk", "text": "x", "citations": [], "cwd": str(repo)},
            headers=headers,
        )
        assert r_missing_instance.status_code == 422


def test_a_cwd_with_a_nul_byte_or_a_symlink_loop_is_refused_cleanly(ctx: AppContext) -> None:
    """A NUL-bearing cwd is invalid input (422), never a lexical match inside a
    project; a cwd through a symlink loop resolves to nothing (422 ``outside_projects``),
    never a 500. Nothing is recorded either way."""
    _project_id, repo = svc_project(ctx)
    mcp_tok = mcp_token(ctx)
    (repo / "loop").symlink_to(repo / "loop")

    with loopback_client(ctx) as client:
        nul = post_finding(
            client, mcp_tok, cwd=f"{repo}/../../../../etc\x00", instance_id="instance-nul"
        )
        assert nul.status_code == 422
        assert nul.json()["error"]["code"] == "invalid_finding"

        loop = post_finding(client, mcp_tok, cwd=str(repo / "loop" / "x"), instance_id="i-loop")
        assert loop.status_code == 422
        assert loop.json()["error"]["code"] == "outside_projects"

        assert client.get("/api/v1/runs").json()["items"] == []


@pytest.mark.asyncio
async def test_no_client_token_tells_the_agent_to_register(ctx: AppContext) -> None:
    _project_id, repo = svc_project(ctx)

    with loopback_client(ctx) as http_client:
        capture_client = interactive_client(
            http_client, token="", cwd=str(repo), instance_id="instance-unregistered"
        )
        async with Client(build_server(capture_client)) as mcp_client:
            result = await mcp_client.call_tool(
                "record_finding", {"kind": "risk", "text": "x", "citations": []}
            )
            assert result.is_error is True
            text = " ".join(getattr(c, "text", "") for c in result.content)
            assert "agenttape mcp register" in text


@pytest.mark.asyncio
async def test_end_to_end_real_stdio_server_records_a_finding_on_a_new_observed_run(
    ctx: AppContext,
) -> None:
    _project_id, repo = svc_project(ctx)
    CaptureTokenStore(ctx.settings.data_dir).mint("mcp", client_copy=True)

    with live_server(ctx) as port:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "agenttape.cli.main", "mcp", "serve"],
            cwd=str(repo),
            env={
                "PATH": os.environ.get("PATH", ""),
                "HOME": os.environ.get("HOME", ""),
                "AGENTTAPE_DATA_DIR": str(ctx.settings.data_dir),
                "AGENTTAPE_API_URL": f"http://127.0.0.1:{port}",
            },
        )
        async with Client(params) as client:
            result = await client.call_tool(
                "record_finding",
                {"kind": "risk", "text": "real stdio interactive finding", "citations": []},
            )
            assert result.is_error is False
            run_id = (result.structured_content or {}).get("run_id")

    kind, source = run_kind_and_source(ctx, run_id)
    assert kind == "observed"
    assert source == "mcp"


def test_a_refused_finding_shows_the_agent_the_reason_without_an_http_status() -> None:
    """A refusal the client has no specific sentence for reaches the agent as AgentTape's
    own message, never prefixed with a bare HTTP status code."""

    def refusing(url: str, body: bytes, headers: Mapping[str, str]) -> tuple[int, bytes]:
        return 409, b'{"error": {"code": "session_mismatch", "message": "M"}}'

    config = CaptureClientConfig(
        api_base="http://ignored",
        token="atc_token",
        attempt_id="",
        run_id="",
        instance_id="instance",
        cwd="/work",
    )
    client = CaptureClient(config, transport=refusing)
    with pytest.raises(CaptureError) as caught:
        client.post_finding(FindingRecordIn(kind="claim", text="a finding"))
    message = str(caught.value)
    assert message.startswith("AgentTape did not record this finding: M")
    assert not message.startswith("409")
