"""Hook-recorded events are redacted with the attempt's secret values, not patterns only.

A Herdr-hosted run sends the agent its resolved prompt, so a
``{{ secret:NAME }}`` value can reach the session, and a hook-recorded read (with its
retained copy on disk, up to 1 MiB) can carry it. A value that matches no credential
pattern (a bare token with no ``key=`` prefix) was stored as is, because the hook ingest
redacted with patterns only. The worker now publishes each running attempt's redactor
(in memory, never persisted) and the hook ingest redacts that attempt's events with it; an
attempt with none registered (an observed session, or a worker in another process) still
gets the values of ``AGENTTAPE_REDACT_ENV``.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from agenttape.adapters.base import (
    AdapterResult,
    AgentAdapter,
    DiscoveryResult,
    ExecutionContext,
    ProfileIssue,
)
from agenttape.application.context import AppContext
from agenttape.application.schemas.capture import FindingRecordIn
from agenttape.application.schemas.catalog import AgentProfileCreate
from agenttape.application.services.capture import CaptureService
from agenttape.application.services.profiles import AgentProfileService
from agenttape.domain.enums import AdapterAvailability
from agenttape.infrastructure.db.models import Event
from agenttape.security.redaction import Redactor
from tests.conftest import drive, start_run
from tests.integration.capture_helpers import (
    hooks_token,
    hosted_attempt,
    loopback_client,
    post_hook,
    post_read,
    pre_read,
    session_start,
    svc_project,
)

#: A bare value no credential pattern matches: only value-based redaction catches it.
BARE_SECRET = "zq7Kp2mW" + "9xR4tY8vB3nL6"
SESSION_ID = "sess-hosted-secret"


def file_with_secret(count: int = 600, at: int = 500) -> str:
    lines = [f"line {n:06d}: the value of item {n} is {n * 7} units" for n in range(1, count + 1)]
    lines[at - 1] = BARE_SECRET
    return "\n".join(lines) + "\n"


def read_result_payload(ctx: AppContext, run_id: str) -> dict[str, Any]:
    with ctx.session() as session:
        [payload] = [
            dict(e.payload or {})
            for e in session.scalars(
                select(Event).where(Event.run_id == run_id, Event.type == "tool_result")
            )
        ]
    return payload


def hook_read(ctx: AppContext, cwd: str, file_path: str, text: str) -> None:
    client = loopback_client(ctx)
    token = hooks_token(ctx)
    for body in (
        session_start(cwd, session_id=SESSION_ID),
        pre_read(cwd, file_path, session_id=SESSION_ID),
        post_read(cwd, file_path, session_id=SESSION_ID, content=text),
    ):
        assert post_hook(client, token, body).status_code == 200


def assert_redacted(ctx: AppContext, run_id: str) -> None:
    payload = read_result_payload(ctx, run_id)
    assert BARE_SECRET not in str(payload)
    kept = ctx.artifact_store.resolve(payload["output_retained"]["storage_path"]).read_bytes()
    assert BARE_SECRET.encode() not in kept
    assert b"[REDACTED:secret]" in kept


def test_a_hosted_runs_hook_read_is_redacted_with_the_attempts_secret_values(
    ctx: AppContext,
) -> None:
    project_id, repo = svc_project(ctx)
    text = file_with_secret()
    (repo / "src" / "big.py").write_text(text)
    run_id, attempt_id = hosted_attempt(ctx, project_id)
    with ctx.attempt_redactors.registered(attempt_id, Redactor([BARE_SECRET])):
        hook_read(ctx, str(repo), str(repo / "src" / "big.py"), text)
    assert_redacted(ctx, run_id)


def test_hook_events_with_no_registered_attempt_use_the_configured_redact_env(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RETAIN_TEST_REDACTED", BARE_SECRET)
    ctx.settings = ctx.settings.model_copy(update={"redact_env": ["RETAIN_TEST_REDACTED"]})
    _project_id, repo = svc_project(ctx)
    text = file_with_secret()
    (repo / "src" / "big.py").write_text(text)
    hook_read(ctx, str(repo), str(repo / "src" / "big.py"), text)
    with ctx.session() as session:
        run_id = session.scalars(select(Event.run_id).where(Event.type == "tool_result")).one()
    assert_redacted(ctx, run_id)


class _SecretProbe(AgentAdapter):
    """Resolves a secret, then reports whether this process's registered redactor for the
    attempt redacts it (the hook ingest's view of the attempt while it runs)."""

    name = "secret_probe"
    display_name = "Secret probe"

    def __init__(self, app: AppContext, seen: dict[str, Any]) -> None:
        self.app = app
        self.seen = seen

    def discover(self) -> DiscoveryResult:
        return DiscoveryResult(adapter=self.name, status=AdapterAvailability.READY)

    def validate_profile(self, config: dict[str, Any]) -> list[ProfileIssue]:
        return []

    async def execute(self, ctx: ExecutionContext) -> AdapterResult:
        value = ctx.resolve_secret("RETAIN_PROBE_SECRET")
        registered = self.app.attempt_redactors.get(ctx.attempt.id)
        self.seen["attempt_id"] = ctx.attempt.id
        self.seen["redacted"] = registered is not None and value not in registered.redact_text(
            f"x {value} y"
        )
        return AdapterResult.success(exit_code=0)


def test_the_worker_registers_each_running_attempts_redactor(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RETAIN_PROBE_SECRET", BARE_SECRET)
    seen: dict[str, Any] = {}
    ctx.adapters.register(_SecretProbe(ctx, seen))
    pid = (
        AgentProfileService(ctx)
        .create(AgentProfileCreate(name="probe", adapter_type="secret_probe", config={}))
        .id
    )
    detail = drive(ctx, start_run(ctx, pid))
    assert detail.run.status == "succeeded", detail.run.failure_message
    assert seen["redacted"] is True
    # Only while the attempt runs: nothing outlives it.
    assert ctx.attempt_redactors.get(seen["attempt_id"]) is None


def test_an_mcp_finding_on_a_hosted_attempt_is_redacted_with_its_secret_values(
    ctx: AppContext,
) -> None:
    project_id, _repo = svc_project(ctx)
    run_id, attempt_id = hosted_attempt(ctx, project_id)
    record = FindingRecordIn(kind="claim", text=f"the config holds {BARE_SECRET} verbatim")
    with ctx.attempt_redactors.registered(attempt_id, Redactor([BARE_SECRET])):
        out = CaptureService(ctx).record_finding(
            run_id=run_id, attempt_id=attempt_id, record=record
        )
    with ctx.session() as session:
        event = session.get(Event, out.event_id)
        assert event is not None
        assert BARE_SECRET not in event.summary
        assert BARE_SECRET not in str(event.payload)
        assert "[REDACTED:secret]" in event.payload["text"]
