"""Attach a Claude Code ``http`` hook session to the run AgentTape already launched
(``attached``) or hosts in Herdr (``hosted``), falling back to an ``observed`` run only
when neither is unambiguous."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from agenttape.application.context import AppContext
from agenttape.application.services.observed_sessions import ObservedSessionService
from agenttape.domain.enums import RunStatus
from agenttape.infrastructure.db.extra_models.observed_sessions import ObservedSession
from agenttape.infrastructure.db.models import Run
from tests.integration.capture_helpers import (
    GREETER_CONTENT,
    hooks_token,
    hosted_attempt,
    loopback_client,
    post_hook,
    post_read,
    pre_read,
    running_attempt,
    session_end,
    session_start,
    svc_project,
)
from tests.integration.helpers import make_repo_project


def _project(ctx: AppContext, name: str) -> tuple[str, Path]:
    """A second git repo (distinct from :func:`svc_project`'s ``svc``), registered as a
    project - used when a test needs two projects with different working directories."""
    project_id = make_repo_project(
        ctx, name=name, project_name=name, files={"src/greeter.py": GREETER_CONTENT}
    )
    return project_id, Path(ctx.settings.allowed_roots[0]) / name


def _set_run(ctx: AppContext, run_id: str, **fields: object) -> None:
    with ctx.session() as session:
        run = session.get(Run, run_id)
        assert run is not None
        for key, value in fields.items():
            setattr(run, key, value)
        session.flush()


def test_a_claimed_running_claude_code_run_attaches_and_suppresses_tool_hooks(
    ctx: AppContext,
) -> None:
    project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)
    attached_run_id, attached_attempt_id = running_attempt(ctx, project_id)
    file_path = str(repo / "src" / "greeter.py")

    r = post_hook(
        client,
        token,
        session_start(str(repo), session_id="sess-attached"),
        **{"X-AgentTape-Run-Id": attached_run_id},
    )
    assert r.status_code == 200 and r.json() == {}

    runs = client.get("/api/v1/runs").json()["items"]
    assert len(runs) == 1
    assert runs[0]["id"] == attached_run_id

    with ctx.session() as session:
        row = session.scalars(select(ObservedSession)).one()
        assert row.mode == "attached"
        assert row.run_id == attached_run_id
        assert row.attempt_id == attached_attempt_id
        assert row.details.get("resolved_by") == "run_id_header"

    post_hook(
        client,
        token,
        pre_read(str(repo), file_path, session_id="sess-attached"),
        **{"X-AgentTape-Run-Id": attached_run_id},
    )
    post_hook(
        client,
        token,
        post_read(str(repo), file_path, session_id="sess-attached"),
        **{"X-AgentTape-Run-Id": attached_run_id},
    )

    events = client.get(f"/api/v1/runs/{attached_run_id}/events").json()["items"]
    assert not any(e["type"] in ("tool_call", "tool_result") for e in events)

    with ctx.session() as session:
        row = session.scalars(select(ObservedSession)).one()
        assert row.details.get("suppressed_tool_hooks") == 2


def test_a_run_id_header_with_a_cwd_outside_the_attempt_workdir_does_not_attach(
    ctx: AppContext,
) -> None:
    """A claim plus a matching working directory, never a claim alone - a
    SessionStart carrying a legitimate running run's id but a cwd outside every project
    (and outside that run's own attempt workdir) must never create an attached row for
    that run."""
    project_id, _repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)
    run_id, _attempt_id = running_attempt(ctx, project_id)

    r = post_hook(
        client,
        token,
        session_start("/tmp/anywhere", session_id="sess-outside-workdir"),
        **{"X-AgentTape-Run-Id": run_id},
    )
    assert r.status_code == 200 and r.json() == {}

    with ctx.session() as session:
        rows = session.scalars(select(ObservedSession)).all()
        assert all(row.mode != "attached" for row in rows)
        assert all(row.run_id != run_id for row in rows)


def test_an_unusable_run_id_header_is_treated_as_absent(ctx: AppContext) -> None:
    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)

    for i, bad in enumerate(["", "${AGENTTAPE_RUN_ID}", "$AGENTTAPE_RUN_ID", "not-a-uuid"]):
        headers = {"X-AgentTape-Run-Id": bad} if bad else {}
        r = post_hook(
            client, token, session_start(str(repo), session_id=f"sess-bad-header-{i}"), **headers
        )
        assert r.status_code == 200 and r.json() == {}

    runs = client.get("/api/v1/runs").json()["items"]
    assert len(runs) == 4
    assert all(run["kind"] == "observed" for run in runs)


def test_finished_demo_and_workflow_runs_are_never_attached(ctx: AppContext) -> None:
    project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)

    finished_run_id, _ = running_attempt(ctx, project_id)
    _set_run(ctx, finished_run_id, status=RunStatus.SUCCEEDED.value)

    demo_run_id, _ = running_attempt(ctx, project_id, adapter_type="demo")

    workflow_run_id, _ = running_attempt(ctx, project_id)
    _set_run(ctx, workflow_run_id, kind="workflow")

    for run_id, session_id in (
        (finished_run_id, "sess-finished"),
        (demo_run_id, "sess-demo"),
        (workflow_run_id, "sess-workflow"),
    ):
        r = post_hook(
            client,
            token,
            session_start(str(repo), session_id=session_id),
            **{"X-AgentTape-Run-Id": run_id},
        )
        assert r.status_code == 200 and r.json() == {}

    runs = client.get("/api/v1/runs").json()["items"]
    observed = [run for run in runs if run["kind"] == "observed"]
    assert len(observed) == 3


def test_a_hosted_herdr_session_records_tool_events_on_the_herdr_attempt(
    ctx: AppContext,
) -> None:
    project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)
    hosted_run_id, hosted_attempt_id = hosted_attempt(ctx, project_id)
    file_path = str(repo / "src" / "greeter.py")

    r = post_hook(client, token, session_start(str(repo), session_id="sess-hosted"))
    assert r.status_code == 200 and r.json() == {}

    runs = client.get("/api/v1/runs").json()["items"]
    assert len(runs) == 1
    assert runs[0]["id"] == hosted_run_id

    post_hook(client, token, pre_read(str(repo), file_path, session_id="sess-hosted"))
    post_hook(client, token, post_read(str(repo), file_path, session_id="sess-hosted"))

    events = client.get(f"/api/v1/runs/{hosted_run_id}/events").json()["items"]
    calls = [e for e in events if e["type"] == "tool_call"]
    results = [e for e in events if e["type"] == "tool_result"]
    assert len(calls) == 1 and len(results) == 1
    assert calls[0]["attempt_id"] == hosted_attempt_id
    assert calls[0]["source"] == "claude-code-hook"
    assert results[0]["source"] == "claude-code-hook"

    with ctx.session() as session:
        row = session.scalars(select(ObservedSession)).one()
        assert row.mode == "hosted"
        assert row.run_id == hosted_run_id
        assert row.details.get("resolved_by") == "cwd_sole_candidate"


def test_a_hosted_session_matches_a_cwd_below_the_attempt_workdir(ctx: AppContext) -> None:
    project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)
    hosted_run_id, _hosted_attempt_id = hosted_attempt(ctx, project_id)

    r = post_hook(client, token, session_start(str(repo / "src"), session_id="sess-hosted-subdir"))
    assert r.status_code == 200 and r.json() == {}

    with ctx.session() as session:
        row = session.scalars(select(ObservedSession)).one()
        assert row.mode == "hosted"
        assert row.run_id == hosted_run_id


def test_two_hosted_candidates_for_the_same_cwd_fall_back_to_observed(ctx: AppContext) -> None:
    project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)
    hosted_attempt(ctx, project_id)
    hosted_attempt(ctx, project_id)

    r = post_hook(client, token, session_start(str(repo), session_id="sess-two-hosted"))
    assert r.status_code == 200 and r.json() == {}

    runs = client.get("/api/v1/runs").json()["items"]
    observed = [run for run in runs if run["kind"] == "observed"]
    assert len(observed) == 1


def test_a_header_naming_a_herdr_run_does_not_host_without_a_workdir_match(
    ctx: AppContext,
) -> None:
    project_a_id, _repo_a = svc_project(ctx)
    project_b_id, repo_b = _project(ctx, "other-svc")
    client = loopback_client(ctx)
    token = hooks_token(ctx)
    herdr_run_id, _herdr_attempt_id = hosted_attempt(ctx, project_a_id)

    r = post_hook(
        client,
        token,
        session_start(str(repo_b), session_id="sess-cross-project"),
        **{"X-AgentTape-Run-Id": herdr_run_id},
    )
    assert r.status_code == 200 and r.json() == {}

    with ctx.session() as session:
        row = session.scalars(select(ObservedSession)).one()
        assert row.mode == "observed"
        assert row.project_id == project_b_id
        assert row.run_id != herdr_run_id


def test_session_end_on_an_attached_or_hosted_row_closes_only_the_row(ctx: AppContext) -> None:
    project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)
    attached_run_id, _ = running_attempt(ctx, project_id)

    post_hook(
        client,
        token,
        session_start(str(repo), session_id="sess-attached-end"),
        **{"X-AgentTape-Run-Id": attached_run_id},
    )
    r = post_hook(
        client,
        token,
        session_end(str(repo), session_id="sess-attached-end"),
        **{"X-AgentTape-Run-Id": attached_run_id},
    )
    assert r.status_code == 200 and r.json() == {}

    detail = client.get(f"/api/v1/runs/{attached_run_id}").json()
    assert detail["run"]["status"] == "running"

    with ctx.session() as session:
        row = session.scalars(select(ObservedSession)).one()
        assert row.status == "closed"
        assert row.close_reason == "session_end"


def test_sweep_closes_an_attached_row_once_its_run_already_ended(ctx: AppContext) -> None:
    project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)
    attached_run_id, _ = running_attempt(ctx, project_id)

    post_hook(
        client,
        token,
        session_start(str(repo), session_id="sess-attached-terminal"),
        **{"X-AgentTape-Run-Id": attached_run_id},
    )
    _set_run(ctx, attached_run_id, status=RunStatus.SUCCEEDED.value)

    closed = ObservedSessionService(ctx).sweep_idle()
    assert closed == []

    with ctx.session() as session:
        row = session.scalars(select(ObservedSession)).one()
        assert row.status == "closed"
        assert row.close_reason == "run_ended"

    detail = client.get(f"/api/v1/runs/{attached_run_id}").json()
    assert detail["run"]["status"] == "succeeded"


def test_sweep_closes_an_idle_hosted_row_without_touching_its_run(ctx: AppContext) -> None:
    project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)
    hosted_run_id, _ = hosted_attempt(ctx, project_id)

    post_hook(client, token, session_start(str(repo), session_id="sess-hosted-idle"))

    with ctx.session() as session:
        row = session.scalars(
            select(ObservedSession).where(ObservedSession.run_id == hosted_run_id)
        ).one()
        row.last_seen_at = ctx.clock.now() - timedelta(hours=2)
        session.flush()

    closed = ObservedSessionService(ctx).sweep_idle()
    assert closed == []

    with ctx.session() as session:
        row = session.scalars(
            select(ObservedSession).where(ObservedSession.run_id == hosted_run_id)
        ).one()
        assert row.status == "closed"
        assert row.close_reason == "idle_window"

    detail = client.get(f"/api/v1/runs/{hosted_run_id}").json()
    assert detail["run"]["status"] == "running"
