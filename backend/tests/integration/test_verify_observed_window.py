"""``never_read`` on an observed Claude Code session: only when AgentTape watched it whole.

An observed run is a session AgentTape did not start; its reads arrive only through
Claude Code ``http`` hooks. AgentTape may say a cited file was "never read" only when it
can show those hooks covered the session from its very start (the first hook it
recorded was ``SessionStart`` with source ``startup``), no hook was dropped, and the
AgentTape process receiving them never changed mid-session. Every other observed run
gets ``read_not_observed``: a missing read proves nothing there. A Claude Code session
hosted in a launched Herdr run is read only through its hooks too, so the same rule
applies to it. A launched Claude Code run (AgentTape owns its stream-json telemetry)
keeps ``never_read``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

import agenttape.api.routers.hooks as hooks_router
import agenttape.application.services.ingest_state as ingest_state
from agenttape.application.context import AppContext
from agenttape.application.services.hook_ingest import hook_ingest
from agenttape.application.services.observed_sessions import (
    OBSERVER_INSTANCE_KEY,
    ObservedSessionService,
)
from agenttape.application.services.runs import RunService
from agenttape.application.services.verification import VerificationService
from agenttape.capture.claude_hooks import HookEvent, session_hash
from agenttape.file_evidence.store import completeness
from agenttape.infrastructure.db.extra_models.observed_sessions import ObservedSession
from agenttape.infrastructure.db.models import Event, Run, RunAttempt
from tests.integration.capture_helpers import (
    hooks_token,
    hosted_attempt,
    loopback_client,
    mcp_token,
    post_hook,
    post_read,
    post_tool_use,
    pre_read,
    session_end,
    session_start,
    svc_project,
)
from tests.integration.helpers import citation_of, finding_record
from tests.integration.test_context_evidence import run_with

SESSION_ID = "sess-observed-window"
QUOTE = "def greet():"


def _record_finding(
    client: TestClient, token: str, cwd: str, *, instance_id: str, quote: str = QUOTE
) -> str:
    """``record_finding`` from an interactive session through the MCP capture endpoint,
    citing ``src/greeter.py`` line 1, a file no hook ever reported a read of."""
    resp = client.post(
        "/api/v1/mcp/findings",
        json={
            "kind": "claim",
            "text": "greet() is defined on line 1",
            "citations": [
                {"path": "src/greeter.py", "line_start": 1, "line_end": 1, "quote": quote}
            ],
            "cwd": cwd,
            "instance_id": instance_id,
        },
        headers={"content-type": "application/json", "authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    run_id: str = resp.json()["run_id"]
    return run_id


def _bash(cwd: str, *, tool_use_id: str) -> dict[str, object]:
    return post_tool_use(
        cwd,
        tool_name="Bash",
        tool_use_id=tool_use_id,
        tool_input={"command": "true"},
        tool_response={"stdout": "", "stderr": "", "interrupted": False},
        session_id=SESSION_ID,
    )


def _verdict(ctx: AppContext, client: TestClient, run_id: str) -> dict[str, Any]:
    """The citation's latest verdict after an explicit re-check of the finished run."""
    VerificationService(ctx).verify_run(run_id)
    verdict: dict[str, Any] = citation_of(client, run_id, _finding_key(client, run_id))["verdict"]
    return verdict


def _finding_key(client: TestClient, run_id: str) -> str:
    resp = client.get(f"/api/v1/runs/{run_id}/context/findings")
    assert resp.status_code == 200, resp.text
    [entry] = resp.json()
    key: str = entry["key"]
    return key


def _observed_session(
    ctx: AppContext,
    *,
    first_hooks: Callable[[str], list[dict[str, object]]],
    before_finding: Callable[[TestClient, str, str], None] | None = None,
    after_finding: Callable[[TestClient, str, str], None] | None = None,
) -> tuple[TestClient, str]:
    """Post ``first_hooks``, optionally run ``before_finding``, record a finding over MCP,
    optionally run ``after_finding``, then end the session. Returns the client and the
    observed run id."""
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    hooks = hooks_token(ctx)
    for body in first_hooks(str(repo)):
        assert post_hook(client, hooks, body).status_code == 200
    if before_finding is not None:
        before_finding(client, hooks, str(repo))
    run_id = _record_finding(client, mcp_token(ctx), str(repo), instance_id="inst-window")
    if after_finding is not None:
        after_finding(client, hooks, str(repo))
    assert (
        post_hook(client, hooks, session_end(str(repo), session_id=SESSION_ID)).status_code == 200
    )
    with ctx.session() as session:
        run = session.get(Run, run_id)
        assert run is not None and run.kind == "observed" and run.status == "succeeded"
    return client, run_id


def test_a_session_hooks_covered_from_startup_says_never_read(ctx: AppContext) -> None:
    client, run_id = _observed_session(
        ctx,
        first_hooks=lambda cwd: [
            session_start(cwd, session_id=SESSION_ID),
            _bash(cwd, tool_use_id="toolu_B1"),
        ],
    )
    verdict = _verdict(ctx, client, run_id)
    assert verdict["verdict"] == "never_read", verdict
    assert verdict["evidence"]["window"]["complete"] is True
    assert verdict["evidence"]["window"]["observed"]["gaps"] == []


def test_an_mcp_only_session_with_no_hooks_is_read_not_observed(ctx: AppContext) -> None:
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    run_id = _record_finding(client, mcp_token(ctx), str(repo), instance_id="inst-mcp-only")
    with ctx.session() as session:
        run = session.get(Run, run_id)
        assert run is not None and run.kind == "observed" and run.source == "mcp"
    RunService(ctx).cancel_run(run_id)

    verdict = _verdict(ctx, client, run_id)
    assert verdict["verdict"] == "read_not_observed", verdict
    window = verdict["evidence"]["window"]
    assert window["complete"] is False
    assert "no_hooks" in window["observed"]["gaps"]
    assert "hooks" in verdict["reason"]


def test_hooks_that_arrived_after_the_session_started_are_read_not_observed(
    ctx: AppContext,
) -> None:
    client, run_id = _observed_session(
        ctx, first_hooks=lambda cwd: [_bash(cwd, tool_use_id="toolu_B1")]
    )
    verdict = _verdict(ctx, client, run_id)
    assert verdict["verdict"] == "read_not_observed", verdict
    assert "joined_mid_session" in verdict["evidence"]["window"]["observed"]["gaps"]


def test_a_resumed_session_is_read_not_observed(ctx: AppContext) -> None:
    client, run_id = _observed_session(
        ctx, first_hooks=lambda cwd: [session_start(cwd, session_id=SESSION_ID, source="resume")]
    )
    verdict = _verdict(ctx, client, run_id)
    assert verdict["verdict"] == "read_not_observed", verdict
    assert "joined_mid_session" in verdict["evidence"]["window"]["observed"]["gaps"]


def test_a_session_begun_by_clear_says_never_read(ctx: AppContext) -> None:
    """``/clear`` starts a new session with an empty context: nothing read before it can
    back a citation made after it, so its ``SessionStart`` is as fresh as ``startup``."""
    client, run_id = _observed_session(
        ctx, first_hooks=lambda cwd: [session_start(cwd, session_id=SESSION_ID, source="clear")]
    )
    verdict = _verdict(ctx, client, run_id)
    assert verdict["verdict"] == "never_read", verdict
    assert verdict["evidence"]["window"]["observed"]["gaps"] == []


def test_dropped_hook_events_are_read_not_observed(ctx: AppContext) -> None:
    def drop_then_resume(client: TestClient, hooks: str, cwd: str) -> None:
        hook_ingest(ctx).mark_dropped(session_hash(SESSION_ID), count=2)
        assert post_hook(client, hooks, _bash(cwd, tool_use_id="toolu_B2")).status_code == 200

    client, run_id = _observed_session(
        ctx,
        first_hooks=lambda cwd: [session_start(cwd, session_id=SESSION_ID)],
        after_finding=drop_then_resume,
    )
    verdict = _verdict(ctx, client, run_id)
    assert verdict["verdict"] == "read_not_observed", verdict
    observed = verdict["evidence"]["window"]["observed"]
    assert "dropped_hook_events" in observed["gaps"]
    assert observed["dropped_hook_events"] == 2


def test_a_read_whose_hooks_failed_to_ingest_is_read_not_observed(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Read's hooks were answered ``{}`` but their ingest raised (a stand-in for
    ``database is locked`` past the busy timeout), before the finding: the hooks are
    counted as dropped, never silently lost behind a complete-looking window."""
    real = ObservedSessionService.ingest_hook

    def locked(self: ObservedSessionService, hook: HookEvent, *, dropped: int = 0) -> Any:
        if hook.tool_name == "Read":
            raise RuntimeError("database is locked")
        return real(self, hook, dropped=dropped)

    def failing_read(cwd: str) -> list[dict[str, object]]:
        monkeypatch.setattr(ObservedSessionService, "ingest_hook", locked)
        file_path = str(Path(cwd) / "src" / "greeter.py")
        return [
            session_start(cwd, session_id=SESSION_ID),
            pre_read(cwd, file_path, session_id=SESSION_ID),
            post_read(cwd, file_path, session_id=SESSION_ID),
        ]

    client, run_id = _observed_session(ctx, first_hooks=failing_read)
    verdict = _verdict(ctx, client, run_id)
    assert verdict["verdict"] == "read_not_observed", verdict
    observed = verdict["evidence"]["window"]["observed"]
    assert "dropped_hook_events" in observed["gaps"]
    assert observed["dropped_hook_events"] == 2


def _warning_sequences(ctx: AppContext, run_id: str, phase: str) -> list[int]:
    with ctx.session() as session:
        return [
            e.sequence
            for e in session.scalars(select(Event).where(Event.run_id == run_id))
            if (e.payload or {}).get("phase") == phase
        ]


def test_hooks_dropped_before_the_finding_are_read_not_observed(ctx: AppContext) -> None:
    """The gap precedes the finding, so it can hide the very read that backs it."""

    def drop_then_resume(client: TestClient, hooks: str, cwd: str) -> None:
        hook_ingest(ctx).mark_dropped(session_hash(SESSION_ID), count=2)
        assert post_hook(client, hooks, _bash(cwd, tool_use_id="toolu_B2")).status_code == 200

    client, run_id = _observed_session(
        ctx,
        first_hooks=lambda cwd: [session_start(cwd, session_id=SESSION_ID)],
        before_finding=drop_then_resume,
    )
    verdict = _verdict(ctx, client, run_id)
    assert verdict["verdict"] == "read_not_observed", verdict
    assert "dropped_hook_events" in verdict["evidence"]["window"]["observed"]["gaps"]
    [warning] = _warning_sequences(ctx, run_id, "hooks_dropped")
    assert warning < _finding_sequence(ctx, run_id)


def test_agenttape_restarting_before_the_finding_is_read_not_observed(ctx: AppContext) -> None:
    def restart(client: TestClient, hooks: str, cwd: str) -> None:
        ctx.extensions.pop(OBSERVER_INSTANCE_KEY, None)  # a fresh process, as after a restart
        assert post_hook(client, hooks, _bash(cwd, tool_use_id="toolu_B3")).status_code == 200

    client, run_id = _observed_session(
        ctx,
        first_hooks=lambda cwd: [session_start(cwd, session_id=SESSION_ID)],
        before_finding=restart,
    )
    verdict = _verdict(ctx, client, run_id)
    assert verdict["verdict"] == "read_not_observed", verdict
    assert "observer_restarted" in verdict["evidence"]["window"]["observed"]["gaps"]
    [warning] = _warning_sequences(ctx, run_id, "observer_restarted")
    assert warning < _finding_sequence(ctx, run_id)


def test_a_restart_noticed_only_by_the_finding_is_recorded_before_it(ctx: AppContext) -> None:
    """No hook reached the new process first: the finding's own resolution notices the
    restart and records it, ahead of the finding."""

    def restart(_client: TestClient, _hooks: str, _cwd: str) -> None:
        ctx.extensions.pop(OBSERVER_INSTANCE_KEY, None)

    client, run_id = _observed_session(
        ctx,
        first_hooks=lambda cwd: [session_start(cwd, session_id=SESSION_ID)],
        before_finding=restart,
    )
    verdict = _verdict(ctx, client, run_id)
    assert verdict["verdict"] == "read_not_observed", verdict
    [warning] = _warning_sequences(ctx, run_id, "observer_restarted")
    assert warning < _finding_sequence(ctx, run_id)


def test_agenttape_restarting_mid_session_is_read_not_observed(ctx: AppContext) -> None:
    """Claude Code's http hooks fail silently while AgentTape is down; the only trace is
    that the next hook arrives at a different AgentTape process than the one before it."""

    def restart(client: TestClient, hooks: str, cwd: str) -> None:
        ctx.extensions.pop(OBSERVER_INSTANCE_KEY, None)  # a fresh process, as after a restart
        assert post_hook(client, hooks, _bash(cwd, tool_use_id="toolu_B3")).status_code == 200

    client, run_id = _observed_session(
        ctx,
        first_hooks=lambda cwd: [session_start(cwd, session_id=SESSION_ID)],
        after_finding=restart,
    )
    verdict = _verdict(ctx, client, run_id)
    assert verdict["verdict"] == "read_not_observed", verdict
    observed = verdict["evidence"]["window"]["observed"]
    assert "observer_restarted" in observed["gaps"]


def test_a_read_whose_hooks_are_still_queued_when_the_finding_arrives_backs_it(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hook ingest runs on its own thread after the hook has been answered, so a Read's
    hooks can still be queued when the ``record_finding`` that cites it arrives. That
    read happened first (Claude Code waits for a hook's answer before it goes on), so it
    must back the citation: the finding is ordered after every hook AgentTape had already
    received, never given an earlier sequence than a read the agent had already done."""
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    hooks = hooks_token(ctx)
    cwd = str(repo)
    file_path = str(repo / "src" / "greeter.py")
    assert post_hook(client, hooks, session_start(cwd, session_id=SESSION_ID)).status_code == 200

    # Hold the ingest thread so the Read's hooks are answered but not yet ingested.
    ingest = hook_ingest(ctx)
    gate = threading.Event()
    held = ingest.in_order(gate.wait)
    for body in (
        pre_read(cwd, file_path, session_id=SESSION_ID),
        post_read(cwd, file_path, session_id=SESSION_ID),
    ):
        assert post_hook(client, hooks, body, drain=False).status_code == 200

    # Know, without a timing guess, when the finding has been handed to the ingest queue.
    queued = threading.Event()
    in_order = ingest.in_order

    def in_order_and_signal(fn: Callable[[], Any]) -> Any:
        future = in_order(fn)
        queued.set()
        return future

    monkeypatch.setattr(ingest, "in_order", in_order_and_signal)
    recorded: list[str] = []
    finding = threading.Thread(
        target=lambda: recorded.append(
            _record_finding(client, mcp_token(ctx), cwd, instance_id="inst-queued")
        )
    )
    finding.start()
    assert queued.wait(timeout=10)
    # Queued behind the held hooks: the request has not been answered and nothing is
    # written yet, so the finding cannot have been sequenced before the Read.
    assert finding.is_alive()
    with ctx.session() as session:
        assert session.scalars(select(Event).where(Event.type == "finding")).first() is None
    gate.set()
    held.result(timeout=10)
    finding.join(timeout=10)
    assert not finding.is_alive()
    assert ingest.drain()
    [run_id] = recorded

    assert post_hook(client, hooks, session_end(cwd, session_id=SESSION_ID)).status_code == 200
    verdict = _verdict(ctx, client, run_id)
    assert verdict["verdict"] == "quote_matches", verdict


def _file_evidence_statement(ctx: AppContext, run_id: str) -> str:
    with ctx.session() as session:
        run = session.get(Run, run_id)
        assert run is not None
        return str(completeness(session, run)["statement"])


def test_file_evidence_says_a_covered_session_read_through_its_hooks(ctx: AppContext) -> None:
    _client, run_id = _observed_session(
        ctx,
        first_hooks=lambda cwd: [
            session_start(cwd, session_id=SESSION_ID),
            _bash(cwd, tool_use_id="toolu_B1"),
        ],
    )
    statement = _file_evidence_statement(ctx, run_id)
    assert "agent telemetry" not in statement
    assert (
        "reads available only from Claude Code hooks, which AgentTape received for the "
        "whole session." in statement
    ), statement


def test_file_evidence_names_the_gap_in_a_session_joined_mid_way(ctx: AppContext) -> None:
    _client, run_id = _observed_session(
        ctx, first_hooks=lambda cwd: [_bash(cwd, tool_use_id="toolu_B1")]
    )
    statement = _file_evidence_statement(ctx, run_id)
    assert "agent telemetry" not in statement
    assert "which did not cover the whole session." in statement, statement
    assert "reached AgentTape only after this session had started" in statement, statement


def test_file_evidence_says_an_mcp_only_session_had_no_hooks(ctx: AppContext) -> None:
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    run_id = _record_finding(client, mcp_token(ctx), str(repo), instance_id="inst-fe-mcp")
    RunService(ctx).cancel_run(run_id)
    statement = _file_evidence_statement(ctx, run_id)
    assert "agent telemetry" not in statement
    assert "Claude Code hooks, which" not in statement
    assert (
        "AgentTape received no Claude Code hooks from this session, so it saw none of its "
        "reads." in statement
    ), statement


def test_a_launched_claude_code_run_keeps_never_read(ctx: AppContext, repo_project: str) -> None:
    """AgentTape owns a launched run's stream-json telemetry, so a run with no recorded
    read of the cited file is still ``never_read``."""
    run_id = run_with(ctx, repo_project, [finding_record("F1", "src/greeter.py", 1, 1, QUOTE)])
    with ctx.session() as session:
        run = session.scalars(select(Run).where(Run.id == run_id)).one()
        assert run.kind == "agent"
        run.adapter_type = "claude-code"
    client = loopback_client(ctx)
    verdict = _verdict(ctx, client, run_id)
    assert verdict["verdict"] == "never_read", verdict
    assert verdict["evidence"]["window"]["complete"] is True
    assert "observed" not in verdict["evidence"]["window"]


# --------------------------------------------------------------------------- Herdr-hosted


def _hosted_session(
    ctx: AppContext, *, first_hooks: Callable[[str], list[dict[str, object]]], restart: bool = False
) -> tuple[TestClient, str]:
    """A Herdr-hosted Claude Code session: its hooks reach a launched ``herdr`` run, and
    they are that run's only source of reads. It reads ``README.md`` (so the run has read
    telemetry at all), records a finding citing ``src/greeter.py`` over MCP, and finishes.

    The worker's File Evidence collector and findings monitor for the attempt are stood in
    for by feeding the run's recorded events through the same evidence path an observed
    run uses (``feed_observed_evidence``: ``telemetry_evidence`` plus ``normalize_events``).
    """
    project_id, repo = svc_project(ctx)
    (repo / "README.md").write_text("# svc\n")
    run_id, attempt_id = hosted_attempt(ctx, project_id)
    client = loopback_client(ctx)
    hooks = hooks_token(ctx)
    cwd = str(repo)
    readme = str(repo / "README.md")
    for body in first_hooks(cwd):
        assert post_hook(client, hooks, body).status_code == 200
    if restart:
        ctx.extensions.pop(OBSERVER_INSTANCE_KEY, None)  # a fresh process, as after a restart
    for body in (
        pre_read(cwd, readme, session_id=SESSION_ID, tool_use_id="toolu_R9"),
        post_read(cwd, readme, session_id=SESSION_ID, tool_use_id="toolu_R9", content="# svc\n"),
    ):
        assert post_hook(client, hooks, body).status_code == 200
    assert _record_finding(client, mcp_token(ctx), cwd, instance_id="inst-hosted") == run_id
    with ctx.session() as session:
        events = list(
            session.scalars(select(Event).where(Event.run_id == run_id).order_by(Event.sequence))
        )
        ObservedSessionService(ctx).feed_observed_evidence(session, run_id, attempt_id, events)
        run = session.get(Run, run_id)
        attempt = session.get(RunAttempt, attempt_id)
        assert run is not None and attempt is not None
        assert run.kind == "agent" and run.adapter_type == "herdr"
        run.status = "succeeded"
        run.finished_at = ctx.clock.now()
        attempt.status = "succeeded"
    return client, run_id


def test_a_hosted_session_covered_from_startup_says_never_read(ctx: AppContext) -> None:
    client, run_id = _hosted_session(
        ctx, first_hooks=lambda cwd: [session_start(cwd, session_id=SESSION_ID)]
    )
    verdict = _verdict(ctx, client, run_id)
    assert verdict["verdict"] == "never_read", verdict
    assert verdict["evidence"]["window"]["observed"]["gaps"] == []
    statement = _file_evidence_statement(ctx, run_id)
    assert "Claude Code hooks, which AgentTape received for the whole session." in statement


def test_a_hosted_session_with_dropped_hooks_is_read_not_observed(ctx: AppContext) -> None:
    def first_hooks(cwd: str) -> list[dict[str, object]]:
        hook_ingest(ctx).mark_dropped(session_hash(SESSION_ID), count=1)
        return [session_start(cwd, session_id=SESSION_ID)]

    client, run_id = _hosted_session(ctx, first_hooks=first_hooks)
    verdict = _verdict(ctx, client, run_id)
    assert verdict["verdict"] == "read_not_observed", verdict
    assert "dropped_hook_events" in verdict["evidence"]["window"]["observed"]["gaps"]
    assert "which did not cover the whole session." in _file_evidence_statement(ctx, run_id)


def test_a_hosted_session_whose_hooks_began_late_is_read_not_observed(ctx: AppContext) -> None:
    client, run_id = _hosted_session(
        ctx, first_hooks=lambda cwd: [_bash(cwd, tool_use_id="toolu_B1")]
    )
    verdict = _verdict(ctx, client, run_id)
    assert verdict["verdict"] == "read_not_observed", verdict
    assert "joined_mid_session" in verdict["evidence"]["window"]["observed"]["gaps"]


def test_a_hosted_session_across_an_agenttape_restart_is_read_not_observed(
    ctx: AppContext,
) -> None:
    client, run_id = _hosted_session(
        ctx, first_hooks=lambda cwd: [session_start(cwd, session_id=SESSION_ID)], restart=True
    )
    verdict = _verdict(ctx, client, run_id)
    assert verdict["verdict"] == "read_not_observed", verdict
    assert "observer_restarted" in verdict["evidence"]["window"]["observed"]["gaps"]


def _hosted_run_with_a_second_session(
    ctx: AppContext,
    *,
    second_hooks: Callable[[str], list[dict[str, object]]],
    restart_between: bool = False,
) -> tuple[TestClient, str]:
    """A Herdr run whose pane hosts two Claude Code sessions, one after the other: the
    first starts fresh, reads ``README.md`` and exits (``SessionEnd`` closes its hosted
    row, not the run); the second (``second_hooks``, session ``SECOND``) then records the
    finding citing ``src/greeter.py``. ``restart_between`` stands in for AgentTape
    restarting after the first session ended and before the second one's first hook."""
    project_id, repo = svc_project(ctx)
    (repo / "README.md").write_text("# svc\n")
    run_id, attempt_id = hosted_attempt(ctx, project_id)
    client = loopback_client(ctx)
    hooks = hooks_token(ctx)
    cwd = str(repo)
    readme = str(repo / "README.md")
    for body in (
        session_start(cwd, session_id=SESSION_ID),
        pre_read(cwd, readme, session_id=SESSION_ID, tool_use_id="toolu_R9"),
        post_read(cwd, readme, session_id=SESSION_ID, tool_use_id="toolu_R9", content="# svc\n"),
        session_end(cwd, session_id=SESSION_ID),
    ):
        assert post_hook(client, hooks, body).status_code == 200
    if restart_between:
        ctx.extensions.pop(OBSERVER_INSTANCE_KEY, None)  # a fresh process, as after a restart
    for body in second_hooks(cwd):
        assert post_hook(client, hooks, body).status_code == 200
    assert _record_finding(client, mcp_token(ctx), cwd, instance_id="inst-second") == run_id
    with ctx.session() as session:
        rows = session.scalars(
            select(ObservedSession).where(ObservedSession.run_id == run_id)
        ).all()
        assert len(rows) == 2 and {r.mode for r in rows} == {"hosted"}
        events = list(
            session.scalars(select(Event).where(Event.run_id == run_id).order_by(Event.sequence))
        )
        ObservedSessionService(ctx).feed_observed_evidence(session, run_id, attempt_id, events)
        run = session.get(Run, run_id)
        attempt = session.get(RunAttempt, attempt_id)
        assert run is not None and attempt is not None
        run.status = "succeeded"
        run.finished_at = ctx.clock.now()
        attempt.status = "succeeded"
    return client, run_id


SECOND = "sess-second"


def test_a_hosted_run_whose_second_session_starts_fresh_says_never_read(ctx: AppContext) -> None:
    client, run_id = _hosted_run_with_a_second_session(
        ctx, second_hooks=lambda cwd: [session_start(cwd, session_id=SECOND)]
    )
    verdict = _verdict(ctx, client, run_id)
    assert verdict["verdict"] == "never_read", verdict
    assert verdict["evidence"]["window"]["observed"]["gaps"] == []


def test_a_hosted_run_whose_second_session_is_resumed_is_read_not_observed(
    ctx: AppContext,
) -> None:
    """``claude --resume`` in the same Herdr pane: the resumed conversation carries reads
    made outside this run, so the first session's fresh start proves nothing about it."""
    client, run_id = _hosted_run_with_a_second_session(
        ctx, second_hooks=lambda cwd: [session_start(cwd, session_id=SECOND, source="resume")]
    )
    verdict = _verdict(ctx, client, run_id)
    assert verdict["verdict"] == "read_not_observed", verdict
    observed = verdict["evidence"]["window"]["observed"]
    assert "joined_mid_session" in observed["gaps"]
    assert observed["first_hook_source"] == "resume"
    assert observed["sessions"] == 2


def test_a_hosted_run_whose_second_session_was_joined_mid_way_is_read_not_observed(
    ctx: AppContext,
) -> None:
    def second(cwd: str) -> list[dict[str, object]]:
        return [
            post_tool_use(
                cwd,
                tool_name="Bash",
                tool_use_id="toolu_S2",
                tool_input={"command": "true"},
                tool_response={"stdout": "", "stderr": "", "interrupted": False},
                session_id=SECOND,
            )
        ]

    client, run_id = _hosted_run_with_a_second_session(ctx, second_hooks=second)
    verdict = _verdict(ctx, client, run_id)
    assert verdict["verdict"] == "read_not_observed", verdict
    assert "joined_mid_session" in verdict["evidence"]["window"]["observed"]["gaps"]


def test_a_hosted_run_whose_sessions_were_observed_by_different_processes_is_read_not_observed(
    ctx: AppContext,
) -> None:
    """The second session's first hook reached a new AgentTape process; nothing on either
    row alone shows a restart, but the run as a whole was not watched by one process."""
    client, run_id = _hosted_run_with_a_second_session(
        ctx,
        second_hooks=lambda cwd: [session_start(cwd, session_id=SECOND)],
        restart_between=True,
    )
    verdict = _verdict(ctx, client, run_id)
    assert verdict["verdict"] == "read_not_observed", verdict
    assert "observer_restarted" in verdict["evidence"]["window"]["observed"]["gaps"]


# ------------------------------------------------- dropped hooks that no later hook carries


class _RefuseReads:
    """Stands in for ``hooks._SESSION_LIMITER``: refuses (429) every hook while
    ``refusing`` is set, so drops go through the router's own 429 path."""

    def __init__(self) -> None:
        self.refusing = False

    def allow(self, _key: str) -> bool:
        return not self.refusing

    def retry_after(self, _key: str) -> float:
        return 1.0


def _refused_read_then_finding(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, str]:
    """A covered session whose Read hooks are refused with 429 by the router, then a
    finding citing the file it read. No later hook from the session reaches AgentTape."""
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    hooks = hooks_token(ctx)
    cwd = str(repo)
    file_path = str(repo / "src" / "greeter.py")
    limiter = _RefuseReads()
    monkeypatch.setattr(hooks_router, "_SESSION_LIMITER", limiter)
    assert post_hook(client, hooks, session_start(cwd, session_id=SESSION_ID)).status_code == 200
    limiter.refusing = True
    for body in (
        pre_read(cwd, file_path, session_id=SESSION_ID),
        post_read(cwd, file_path, session_id=SESSION_ID),
    ):
        assert post_hook(client, hooks, body).status_code == 429
    limiter.refusing = False
    run_id = _record_finding(client, mcp_token(ctx), cwd, instance_id="inst-refused")
    return client, run_id


def _finding_sequence(ctx: AppContext, run_id: str) -> int:
    with ctx.session() as session:
        finding = session.scalars(
            select(Event).where(Event.run_id == run_id, Event.type == "finding")
        ).one()
        return finding.sequence


def test_drops_before_a_finding_are_recorded_before_it(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refused Read's count is written onto the row, with its warning, in the same
    transaction as the finding and sequenced before it, not left in process memory."""
    _client, run_id = _refused_read_then_finding(ctx, monkeypatch)
    finding_sequence = _finding_sequence(ctx, run_id)
    with ctx.session() as session:
        row = session.scalars(select(ObservedSession).where(ObservedSession.run_id == run_id)).one()
        assert row.details.get("dropped_hook_events") == 2
        warnings = [
            e
            for e in session.scalars(select(Event).where(Event.run_id == run_id))
            if (e.payload or {}).get("phase") == "hooks_dropped"
        ]
        assert [w.sequence < finding_sequence for w in warnings] == [True]
    assert hook_ingest(ctx).take_dropped(session_hash(SESSION_ID)) == 0


def test_drops_survive_an_idle_close_with_no_later_hook(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flood lasted until Claude Code was killed: no SessionEnd, no later hook. The
    idle sweep closes and verifies the run, and the refused Read still shows."""
    client, run_id = _refused_read_then_finding(ctx, monkeypatch)
    with ctx.session() as session:
        session.execute(
            update(ObservedSession).values(last_seen_at=ctx.clock.now() - timedelta(days=1))
        )
    closed = ObservedSessionService(ctx).sweep_idle()
    assert [c[0] for c in closed] == [run_id]
    verdict = _verdict(ctx, client, run_id)
    assert verdict["verdict"] == "read_not_observed", verdict
    assert verdict["evidence"]["window"]["observed"]["dropped_hook_events"] == 2


def test_a_drop_left_pending_when_the_idle_sweep_closes_a_session_is_recorded(
    ctx: AppContext,
) -> None:
    """A hook refused after the last finding and never followed by another: the sweep
    that closes the row writes it onto the row rather than dropping it with the process
    memory."""
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    hooks = hooks_token(ctx)
    assert (
        post_hook(client, hooks, session_start(str(repo), session_id=SESSION_ID)).status_code == 200
    )
    hook_ingest(ctx).mark_dropped(session_hash(SESSION_ID), count=3)
    with ctx.session() as session:
        session.execute(
            update(ObservedSession).values(last_seen_at=ctx.clock.now() - timedelta(days=1))
        )
    [(run_id, _status)] = ObservedSessionService(ctx).sweep_idle()
    with ctx.session() as session:
        row = session.scalars(select(ObservedSession).where(ObservedSession.run_id == run_id)).one()
        assert row.status == "closed"
        assert row.details.get("dropped_hook_events") == 3


def test_a_hosted_row_closed_idle_keeps_its_pending_drops(ctx: AppContext) -> None:
    project_id, repo = svc_project(ctx)
    run_id, _attempt_id = hosted_attempt(ctx, project_id)
    client = loopback_client(ctx)
    hooks = hooks_token(ctx)
    assert (
        post_hook(client, hooks, session_start(str(repo), session_id=SESSION_ID)).status_code == 200
    )
    hook_ingest(ctx).mark_dropped(session_hash(SESSION_ID), count=1)
    with ctx.session() as session:
        session.execute(
            update(ObservedSession).values(last_seen_at=ctx.clock.now() - timedelta(days=1))
        )
    ObservedSessionService(ctx).sweep_idle()
    with ctx.session() as session:
        row = session.scalars(select(ObservedSession).where(ObservedSession.run_id == run_id)).one()
        assert (row.status, row.close_reason) == ("closed", "idle_window")
        assert row.details.get("dropped_hook_events") == 1


def test_an_evicted_pending_drop_marks_every_open_session_instead_of_vanishing(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past ``MAX_TRACKED_DROPPED_SESSIONS`` the oldest count can no longer be kept for
    its session; it becomes an unattributed drop, written onto every open observed or
    hosted session before the next hook or finding, so none of them looks whole."""
    monkeypatch.setattr(ingest_state, "MAX_TRACKED_DROPPED_SESSIONS", 1)
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    hooks = hooks_token(ctx)
    cwd = str(repo)
    assert post_hook(client, hooks, session_start(cwd, session_id=SESSION_ID)).status_code == 200
    ingest = hook_ingest(ctx)
    ingest.mark_dropped(session_hash(SESSION_ID), count=2)
    ingest.mark_dropped(session_hash("sess-flood-1"))  # evicts SESSION_ID's count
    assert ingest.take_dropped(session_hash(SESSION_ID)) == 0
    run_id = _record_finding(client, mcp_token(ctx), cwd, instance_id="inst-evicted")
    assert post_hook(client, hooks, session_end(cwd, session_id=SESSION_ID)).status_code == 200
    verdict = _verdict(ctx, client, run_id)
    assert verdict["verdict"] == "read_not_observed", verdict
    assert verdict["evidence"]["window"]["observed"]["dropped_hook_events"] == 2


def test_drops_lost_with_a_restart_still_leave_a_gap(ctx: AppContext) -> None:
    """Pending counts live in process memory. A restart loses them, but the next hook or
    finding reaching the new process records the restart, which is a gap on its own."""
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    hooks = hooks_token(ctx)
    cwd = str(repo)
    assert post_hook(client, hooks, session_start(cwd, session_id=SESSION_ID)).status_code == 200
    hook_ingest(ctx).mark_dropped(session_hash(SESSION_ID), count=2)
    # A fresh process: no pending counts, a new observer id.
    ctx.extensions.pop("pending_hook_drops", None)
    ctx.extensions.pop(OBSERVER_INSTANCE_KEY, None)
    run_id = _record_finding(client, mcp_token(ctx), cwd, instance_id="inst-restart")
    assert post_hook(client, hooks, session_end(cwd, session_id=SESSION_ID)).status_code == 200
    verdict = _verdict(ctx, client, run_id)
    assert verdict["verdict"] == "read_not_observed", verdict
    assert "observer_restarted" in verdict["evidence"]["window"]["observed"]["gaps"]


def test_a_covered_hosted_session_that_read_nothing_says_never_read(ctx: AppContext) -> None:
    """Its hooks reported every tool call and none was a read, so the cited file
    was not read under full observation: ``never_read``, and File Evidence says the reads
    came from Claude Code hooks, not "Agent telemetry unavailable"."""
    project_id, repo = svc_project(ctx)
    run_id, attempt_id = hosted_attempt(ctx, project_id)
    client = loopback_client(ctx)
    hooks = hooks_token(ctx)
    cwd = str(repo)
    assert post_hook(client, hooks, session_start(cwd, session_id=SESSION_ID)).status_code == 200
    assert _record_finding(client, mcp_token(ctx), cwd, instance_id="inst-no-reads") == run_id
    with ctx.session() as session:
        events = list(
            session.scalars(select(Event).where(Event.run_id == run_id).order_by(Event.sequence))
        )
        ObservedSessionService(ctx).feed_observed_evidence(session, run_id, attempt_id, events)
        run = session.get(Run, run_id)
        attempt = session.get(RunAttempt, attempt_id)
        assert run is not None and attempt is not None
        run.status = "succeeded"
        run.finished_at = ctx.clock.now()
        attempt.status = "succeeded"
    verdict = _verdict(ctx, client, run_id)
    assert verdict["verdict"] == "never_read", verdict
    assert verdict["evidence"]["window"]["reads"] == "telemetry"
    statement = _file_evidence_statement(ctx, run_id)
    assert "Agent telemetry unavailable" not in statement
    assert "Claude Code hooks, which AgentTape received for the whole session." in statement
