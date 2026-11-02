"""End-to-end MCP capture: a real ``agenttape mcp serve`` stdio process reaches the
findings API for a running claude-code attempt."""

from __future__ import annotations

from collections.abc import Iterator, Mapping

import pytest
from fastapi.testclient import TestClient
from mcp import Client
from sqlalchemy import select

from agenttape.api.app import create_app
from agenttape.api.routers import mcp as mcp_router
from agenttape.application.context import AppContext
from agenttape.application.events import EventRecorder
from agenttape.context_evidence.hooks import rescan_run
from agenttape.domain.enums import EventType
from agenttape.infrastructure.db.extra_models.findings import Citation, Finding
from agenttape.infrastructure.db.models import Event, RunAttempt
from agenttape.mcp.client import CaptureClient, CaptureClientConfig, CaptureError
from agenttape.mcp.server import build_server
from agenttape.security.capture import mint_attempt_token
from tests.integration.capture_helpers import (
    event_count,
    live_server,
    loopback_client,
    mcp_token,
    running_attempt,
    stdio_params,
    svc_project,
    to_thread_transport,
)

#: A fake AWS-shaped secret, assembled at runtime so the secret scan does not flag the test.
LEAK = "AKIA" + "QQQQWWWWEEEERRRR"


def _record_tool_call_and_result(ctx: AppContext, run_id: str, attempt_id: str) -> None:
    recorder = EventRecorder(ctx.settings, ctx.clock)
    with ctx.session() as session:
        call = recorder.record(
            session,
            run_id,
            EventType.TOOL_CALL,
            "Bash",
            payload={"tool_use_id": "toolu_T1", "name": "Bash", "input": {"command": "pytest"}},
            source="claude-code",
            attempt_id=attempt_id,
        )
        recorder.record(
            session,
            run_id,
            EventType.TOOL_RESULT,
            "ok 3 passed",
            payload={"tool_use_id": "toolu_T1", "output": "ok 3 passed"},
            source="claude-code",
            attempt_id=attempt_id,
            parent_event_id=call.id,
        )


@pytest.mark.asyncio
async def test_record_finding_over_stdio_reaches_the_findings_api(ctx: AppContext) -> None:
    project_id, _repo = svc_project(ctx)
    run_id, attempt_id = running_attempt(ctx, project_id)
    _record_tool_call_and_result(ctx, run_id, attempt_id)
    token = mint_attempt_token(ctx.settings.data_dir, attempt_id)

    with live_server(ctx) as port:
        params = stdio_params(port, run_id, attempt_id, token, str(ctx.settings.data_dir))
        async with Client(params) as client:
            tools = await client.list_tools()
            names = [t.name for t in tools.tools]
            assert "record_finding" in names
            record_tool = next(t for t in tools.tools if t.name == "record_finding")
            # What an interactive session sees instead of the launched-run contract.
            assert "Grep or Glob output is not a file read" in (record_tool.description or "")
            schema_props = record_tool.input_schema.get("properties", {})
            assert {"kind", "text", "citations"} <= set(schema_props)

            result = await client.call_tool(
                "record_finding",
                {
                    "kind": "risk",
                    "text": "greet() ignores its caller",
                    "citations": [
                        {
                            "path": "src/greeter.py",
                            "line_start": 1,
                            "line_end": 2,
                            "quote": "def greet():\n    return 'hi'",
                        },
                        {"event_ref": "toolu_T1", "quote": "ok 3 passed"},
                    ],
                },
            )
            assert result.is_error is False
            text = " ".join(getattr(c, "text", "") for c in result.content)
            assert run_id in text or (result.structuredContent or {}).get("run_id") == run_id

    rescan_run(ctx, run_id)
    with loopback_client(ctx) as client:
        response = client.get(f"/api/v1/runs/{run_id}/context/findings")
        assert response.status_code == 200
        findings = response.json()
        assert len(findings) == 1
        finding = findings[0]
        assert finding["source"] == "agenttape-mcp"
        assert finding["kind"] == "risk"
        citations = finding["citations"]
        assert len(citations) == 2
        file_cite = next(c for c in citations if c["kind"] == "file")
        assert file_cite["path"] == "src/greeter.py"
        assert file_cite["inside_boundary"] is True
        assert file_cite["line_start"] == 1 and file_cite["line_end"] == 2
        event_cite = next(c for c in citations if c["kind"] == "event")
        assert event_cite["event_id"] is not None


@pytest.mark.asyncio
async def test_record_finding_passes_a_review_target_through_unchanged(ctx: AppContext) -> None:
    project_id, _repo = svc_project(ctx)
    run_id, attempt_id = running_attempt(ctx, project_id)
    other_run_id, _other_attempt_id = running_attempt(ctx, project_id)
    token = mint_attempt_token(ctx.settings.data_dir, attempt_id)

    with live_server(ctx) as port:
        params = stdio_params(port, run_id, attempt_id, token, str(ctx.settings.data_dir))
        async with Client(params) as client:
            result = await client.call_tool(
                "record_finding",
                {
                    "kind": "risk",
                    "text": "challenging another run's finding",
                    "citations": [{"path": "src/greeter.py", "quote": "def greet():"}],
                    "target": {"run_id": other_run_id, "finding_key": "F2", "outcome": "refuted"},
                },
            )
            assert result.is_error is False

    with ctx.session() as session:
        event = session.scalars(
            select(Event).where(Event.run_id == run_id, Event.type == "finding")
        ).one()
        assert event.payload["target"] == {
            "run_id": other_run_id,
            "finding_key": "F2",
            "outcome": "refuted",
        }

    with live_server(ctx) as port:
        params = stdio_params(port, run_id, attempt_id, token, str(ctx.settings.data_dir))
        async with Client(params) as client:
            await client.call_tool(
                "record_finding",
                {
                    "kind": "note",
                    "text": "no target here",
                    "citations": [{"path": "src/greeter.py", "quote": "def greet():"}],
                },
            )

    with ctx.session() as session:
        untargeted = session.scalars(
            select(Event)
            .where(Event.run_id == run_id, Event.type == "finding")
            .order_by(Event.sequence.desc())
        ).first()
        assert untargeted is not None
        assert "target" not in untargeted.payload


@pytest.mark.asyncio
async def test_record_finding_rejects_an_unknown_review_outcome(ctx: AppContext) -> None:
    project_id, _repo = svc_project(ctx)
    run_id, attempt_id = running_attempt(ctx, project_id)
    other_run_id, _ = running_attempt(ctx, project_id)
    token = mint_attempt_token(ctx.settings.data_dir, attempt_id)

    with live_server(ctx) as port:
        params = stdio_params(port, run_id, attempt_id, token, str(ctx.settings.data_dir))
        async with Client(params) as client:
            result = await client.call_tool(
                "record_finding",
                {
                    "kind": "risk",
                    "text": "bad outcome",
                    "citations": [{"path": "src/greeter.py", "quote": "def greet():"}],
                    "target": {"run_id": other_run_id, "finding_key": "F2", "outcome": "maybe"},
                },
            )
            assert result.is_error is True

    with ctx.session() as session:
        count = session.execute(
            select(Event).where(Event.run_id == run_id, Event.type == "finding")
        ).all()
        assert len(count) == 0


# --------------------------------------------------------------------------- hardening (Task 3)


def test_non_loopback_peer_is_rejected(ctx: AppContext) -> None:
    app = create_app(ctx, web_dist=ctx.settings.data_dir / "no-web-dist")
    with TestClient(app, base_url="http://127.0.0.1", client=("203.0.113.9", 50000)) as client:
        before = event_count(ctx)
        response = client.post(
            "/api/v1/mcp/findings",
            json={"kind": "risk", "text": "x", "citations": []},
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "not_loopback"
        assert event_count(ctx) == before


def test_untrusted_host_header_is_rejected(ctx: AppContext) -> None:
    with loopback_client(ctx) as client:
        response = client.post(
            "/api/v1/mcp/findings",
            json={"kind": "risk", "text": "x", "citations": []},
            headers={"content-type": "application/json", "host": "evil.example.com"},
        )
        assert response.status_code == 421
        assert response.json()["error"]["code"] == "untrusted_host"


@pytest.mark.parametrize(
    ("header", "value"), [("origin", "http://evil.example.com"), ("sec-fetch-site", "cross-site")]
)
def test_browser_shaped_headers_are_rejected(ctx: AppContext, header: str, value: str) -> None:
    with loopback_client(ctx) as client:
        response = client.post(
            "/api/v1/mcp/findings",
            json={"kind": "risk", "text": "x", "citations": []},
            headers={"content-type": "application/json", header: value},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "browser_request"


@pytest.mark.parametrize("header", ["forwarded", "x-forwarded-for", "x-real-ip"])
def test_forwarding_headers_are_rejected(ctx: AppContext, header: str) -> None:
    with loopback_client(ctx) as client:
        response = client.post(
            "/api/v1/mcp/findings",
            json={"kind": "risk", "text": "x", "citations": []},
            headers={"content-type": "application/json", header: "1.2.3.4"},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "proxied_request"


def test_wrong_content_type_is_rejected(ctx: AppContext) -> None:
    with loopback_client(ctx) as client:
        response = client.post(
            "/api/v1/mcp/findings", content=b"not json", headers={"content-type": "text/plain"}
        )
        assert response.status_code == 415


def test_content_length_over_the_cap_is_rejected(ctx: AppContext) -> None:
    with loopback_client(ctx) as client:
        response = client.post(
            "/api/v1/mcp/findings",
            content=b"x" * (mcp_router.MAX_MCP_BODY_BYTES + 1024),
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {mcp_token(ctx)}",
            },
        )
        assert response.status_code == 413


def test_chunked_body_over_the_cap_is_rejected(ctx: AppContext) -> None:
    def chunks() -> Iterator[bytes]:
        for _ in range(300):
            yield b"x" * 1024

    with loopback_client(ctx) as client:
        response = client.post(
            "/api/v1/mcp/findings",
            content=chunks(),
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {mcp_token(ctx)}",
            },
        )
        assert response.status_code == 413


@pytest.mark.parametrize(
    "citations",
    [
        [{"path": "a.py", "event_ref": "toolu_1", "quote": "x"}],
        [{"bogus": "x"}],
        [{"path": "a.py", "quote": "x"}] * 51,
        [{"path": "a.py", "quote": ""}],
    ],
)
def test_invalid_citations_are_rejected(
    ctx: AppContext, citations: list[dict[str, object]]
) -> None:
    with loopback_client(ctx) as client:
        before = event_count(ctx)
        response = client.post(
            "/api/v1/mcp/findings",
            json={"kind": "risk", "text": "x", "citations": citations},
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {mcp_token(ctx)}",
            },
        )
        assert response.status_code == 422
        assert event_count(ctx) == before


def test_finding_too_large_is_rejected_without_a_silent_truncation(ctx: AppContext) -> None:
    huge_citations = [
        {"path": "a.py", "quote": "q" * 4000, "line_start": 1, "line_end": 2} for _ in range(50)
    ]
    with loopback_client(ctx) as client:
        before = event_count(ctx)
        response = client.post(
            "/api/v1/mcp/findings",
            json={"kind": "risk", "text": "x" * 4000, "citations": huge_citations},
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {mcp_token(ctx)}",
            },
        )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "finding_too_large"
        assert event_count(ctx) == before


def test_unauthenticated_oversized_body_is_rejected_before_being_read_or_parsed(
    ctx: AppContext,
) -> None:
    """An oversized or invalid-shaped body from a caller with no (or the
    wrong) capture credential answers 401, not 413/422 - the body is never streamed, JSON
    parsed, or Pydantic-validated before authentication, mirroring ``claude_code_hook``."""
    with loopback_client(ctx) as client:
        before = event_count(ctx)
        response = client.post(
            "/api/v1/mcp/findings",
            content=b"x" * (mcp_router.MAX_MCP_BODY_BYTES + 1024),
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_capture_token"
        assert event_count(ctx) == before

        wrong_token_response = client.post(
            "/api/v1/mcp/findings",
            json={"kind": "risk", "text": "x", "citations": [{"bogus": "x"}]},
            headers={
                "content-type": "application/json",
                "authorization": "Bearer atc_wrong",
            },
        )
        assert wrong_token_response.status_code == 401
        assert wrong_token_response.json()["error"]["code"] == "invalid_capture_token"
        assert event_count(ctx) == before


def test_missing_or_wrong_token_variants_are_all_401(ctx: AppContext) -> None:
    project_id, _repo = svc_project(ctx)
    _run_id, attempt_id = running_attempt(ctx, project_id)
    _other_run_id, other_attempt_id = running_attempt(ctx, project_id)
    token = mint_attempt_token(ctx.settings.data_dir, attempt_id)
    other_token = mint_attempt_token(ctx.settings.data_dir, other_attempt_id)
    _non_agent_run_id, non_agent_attempt_id = running_attempt(
        ctx, project_id, adapter_type="generic-command"
    )
    non_agent_token = mint_attempt_token(ctx.settings.data_dir, non_agent_attempt_id)

    with ctx.session() as session:
        stopped = session.get(RunAttempt, other_attempt_id)
        assert stopped is not None
        stopped.status = "succeeded"

    body = {"kind": "risk", "text": "x", "citations": []}

    with loopback_client(ctx) as client:
        before = event_count(ctx)

        r1 = client.post(
            "/api/v1/mcp/findings",
            json=body,
            headers={"content-type": "application/json", "x-agenttape-attempt": attempt_id},
        )
        r2 = client.post(
            "/api/v1/mcp/findings",
            json=body,
            headers={
                "content-type": "application/json",
                "x-agenttape-attempt": attempt_id,
                "authorization": "Bearer wrong-token-value",
            },
        )
        r3 = client.post(
            "/api/v1/mcp/findings",
            json=body,
            headers={
                "content-type": "application/json",
                "x-agenttape-attempt": other_attempt_id,
                "authorization": f"Bearer {token}",
            },
        )
        r4 = client.post(
            "/api/v1/mcp/findings",
            json=body,
            headers={
                "content-type": "application/json",
                "x-agenttape-attempt": other_attempt_id,
                "authorization": f"Bearer {other_token}",
            },
        )
        r5 = client.post(
            "/api/v1/mcp/findings",
            json=body,
            headers={
                "content-type": "application/json",
                "x-agenttape-attempt": non_agent_attempt_id,
                "authorization": f"Bearer {non_agent_token}",
            },
        )

        for r in (r1, r2, r3, r4, r5):
            assert r.status_code == 401
            assert r.json()["error"]["code"] == "invalid_capture_token"
        assert event_count(ctx) == before


def test_per_attempt_write_burst_is_rate_limited(ctx: AppContext) -> None:
    project_id, _repo = svc_project(ctx)
    _run_id, attempt_id = running_attempt(ctx, project_id)
    token = mint_attempt_token(ctx.settings.data_dir, attempt_id)
    headers = {
        "content-type": "application/json",
        "x-agenttape-attempt": attempt_id,
        "authorization": f"Bearer {token}",
    }
    with loopback_client(ctx) as client:
        statuses = []
        for _ in range(65):
            r = client.post(
                "/api/v1/mcp/findings",
                json={"kind": "risk", "text": "x", "citations": []},
                headers=headers,
            )
            statuses.append(r.status_code)
        assert 429 in statuses


def test_auth_failures_are_rate_limited_per_client(ctx: AppContext) -> None:
    with loopback_client(ctx) as client:
        statuses = []
        last = None
        for _ in range(25):
            last = client.post(
                "/api/v1/mcp/findings",
                json={"kind": "risk", "text": "x", "citations": []},
                headers={"content-type": "application/json"},
            )
            statuses.append(last.status_code)
        assert statuses.count(401) == 20
        assert statuses.count(429) == 5
        assert last is not None
        assert "retry-after" in {k.lower() for k in last.headers}


@pytest.mark.asyncio
async def test_planted_secret_is_redacted_everywhere(ctx: AppContext) -> None:
    project_id, repo = svc_project(ctx)
    run_id, attempt_id = running_attempt(ctx, project_id)
    token = mint_attempt_token(ctx.settings.data_dir, attempt_id)

    with loopback_client(ctx) as http_client:
        config = CaptureClientConfig(
            api_base="http://ignored",
            token=token,
            attempt_id=attempt_id,
            run_id=run_id,
            instance_id="test",
            cwd=str(repo),
        )
        capture_client = CaptureClient(config, transport=to_thread_transport(http_client))
        server = build_server(capture_client)
        async with Client(server) as mcp_client:
            result = await mcp_client.call_tool(
                "record_finding",
                {
                    "kind": "risk",
                    "text": f"leaked key {LEAK}",
                    "citations": [{"path": "src/greeter.py", "quote": f"key={LEAK}"}],
                },
            )
            assert result.is_error is False

    rescan_run(ctx, run_id)
    with ctx.session() as session:
        events_blob = " ".join(
            str(e.payload) for e in session.scalars(select(Event).where(Event.run_id == run_id))
        )
        finding = session.scalars(select(Finding).where(Finding.run_id == run_id)).one()
        citation = session.scalars(select(Citation).where(Citation.finding_id == finding.id)).one()
    assert LEAK not in events_blob
    assert LEAK not in (finding.text or "")
    assert LEAK not in (citation.quote or "")
    assert "[REDACTED:" in events_blob
    assert "[REDACTED:" in (finding.text or "")
    assert "[REDACTED:" in (citation.quote or "")


@pytest.mark.asyncio
async def test_tool_errors_are_readable_for_each_failure_mode(ctx: AppContext) -> None:
    project_id, _repo = svc_project(ctx)
    run_id, attempt_id = running_attempt(ctx, project_id)
    token = mint_attempt_token(ctx.settings.data_dir, attempt_id)

    def make_config(this_token: str) -> CaptureClientConfig:
        return CaptureClientConfig(
            api_base="http://ignored",
            token=this_token,
            attempt_id=attempt_id,
            run_id=run_id,
            instance_id="test",
            cwd="/",
        )

    with loopback_client(ctx) as http_client:
        wrong_token_client = CaptureClient(
            make_config("atm_wrong"), transport=to_thread_transport(http_client)
        )
        async with Client(build_server(wrong_token_client)) as c1:
            r1 = await c1.call_tool(
                "record_finding", {"kind": "risk", "text": "x", "citations": []}
            )
            assert r1.is_error is True
            text1 = " ".join(getattr(b, "text", "") for b in r1.content).lower()
            assert "capture token" in text1

        real_client = CaptureClient(make_config(token), transport=to_thread_transport(http_client))
        async with Client(build_server(real_client)) as c2:
            huge_citations = [
                {"path": "a.py", "quote": "q" * 4000, "line_start": 1, "line_end": 2}
                for _ in range(50)
            ]
            r2 = await c2.call_tool(
                "record_finding",
                {"kind": "risk", "text": "x" * 4000, "citations": huge_citations},
            )
            assert r2.is_error is True
            text2 = " ".join(getattr(b, "text", "") for b in r2.content).lower()
            assert "split" in text2 or "smaller" in text2

        def canned_422(_url: str, _body: bytes, _headers: Mapping[str, str]) -> tuple[int, bytes]:
            return 422, b'{"error": {"code": "invalid_finding", "message": "bad shape"}}'

        canned_client = CaptureClient(make_config(token), transport=canned_422)
        async with Client(build_server(canned_client)) as c3:
            r3 = await c3.call_tool(
                "record_finding", {"kind": "risk", "text": "x", "citations": []}
            )
            assert r3.is_error is True
            assert "bad shape" in " ".join(getattr(b, "text", "") for b in r3.content)

        def canned_429(_url: str, _body: bytes, _headers: Mapping[str, str]) -> tuple[int, bytes]:
            return 429, b'{"error": {"code": "rate_limited", "message": "slow down"}}'

        canned_client_429 = CaptureClient(make_config(token), transport=canned_429)
        async with Client(build_server(canned_client_429)) as c4:
            r4 = await c4.call_tool(
                "record_finding", {"kind": "risk", "text": "x", "citations": []}
            )
            assert r4.is_error is True
            assert "slow down" in " ".join(getattr(b, "text", "") for b in r4.content)

        def unreachable(_url: str, _body: bytes, _headers: Mapping[str, str]) -> tuple[int, bytes]:
            raise CaptureError("AgentTape is not reachable at http://ignored")

        unreachable_client = CaptureClient(make_config(token), transport=unreachable)
        async with Client(build_server(unreachable_client)) as c5:
            r5 = await c5.call_tool(
                "record_finding", {"kind": "risk", "text": "x", "citations": []}
            )
            assert r5.is_error is True
            assert "not reachable" in " ".join(getattr(b, "text", "") for b in r5.content)
