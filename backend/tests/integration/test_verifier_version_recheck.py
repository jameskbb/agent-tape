"""A verifier change re-checks the hook-read runs whose ``never_read`` it may overturn.

``VERIFIER_VERSION`` "2" requires an observed or Herdr-hosted Claude Code session's hooks to
have covered it whole before a missing read is ``never_read``. A verdict recorded by an
older verifier was decided without that check, and verification runs automatically only
when a run finishes, so without a re-check a finished run would keep its old
``never_read`` as its latest verdict. ``recheck_outdated_never_read`` re-verifies exactly
those runs (``agenttape serve`` runs it once at start); a launched run's verdicts are
unaffected by the change and are left alone.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from agenttape.application.context import AppContext
from agenttape.application.services.findings import FindingsService, latest_verdict
from agenttape.application.services.observed_sessions import ObservedSessionService
from agenttape.application.services.verification import VerificationService
from agenttape.findings.verify import METHOD, VERIFIER_VERSION
from agenttape.infrastructure.db.extra_models.findings import Citation, CitationVerdict, Finding
from agenttape.infrastructure.db.models import Event, Run, RunAttempt
from agenttape.runtime.services.observed_sessions import ObservedSessionSweeper
from tests.integration.capture_helpers import (
    hooks_token,
    hosted_attempt,
    loopback_client,
    mcp_token,
    post_hook,
    post_tool_use,
    session_end,
    session_start,
    svc_project,
)
from tests.integration.helpers import finding_record
from tests.integration.test_context_evidence import run_with
from tests.integration.test_verify_observed_window import _record_finding


def _bash(cwd: str, session_id: str) -> dict[str, object]:
    return post_tool_use(
        cwd,
        tool_name="Bash",
        tool_use_id="toolu_B1",
        tool_input={"command": "true"},
        tool_response={"stdout": "", "stderr": "", "interrupted": False},
        session_id=session_id,
    )


def _citation_id(ctx: AppContext, run_id: str) -> str:
    with ctx.session() as session:
        return session.scalars(
            select(Citation.id)
            .join(Finding, Finding.id == Citation.finding_id)
            .where(Finding.run_id == run_id)
        ).one()


def _old_never_read(ctx: AppContext, run_id: str) -> str:
    """Make what verifier "1" decided the citation's latest verdict: ``never_read``, with
    no hook-coverage check at all."""
    citation_id = _citation_id(ctx, run_id)
    FindingsService(ctx).record_verdict(
        citation_id,
        verdict="never_read",
        method=METHOD,
        reason="This run has no recorded read of src/greeter.py.",
        evidence={"rule": METHOD, "inputs_digest": "0" * 64},
        verifier_version="1",
    )
    return citation_id


def _latest(ctx: AppContext, citation_id: str) -> CitationVerdict:
    with ctx.session() as session:
        rows = list(
            session.scalars(
                select(CitationVerdict).where(CitationVerdict.citation_id == citation_id)
            )
        )
        latest = latest_verdict(rows)
        assert latest is not None
        session.expunge(latest)
        return latest


def _observed_run_joined_mid_session(ctx: AppContext, repo: str) -> str:
    client = loopback_client(ctx)
    hooks = hooks_token(ctx)
    assert post_hook(client, hooks, _bash(repo, "sess-old")).status_code == 200
    run_id = _record_finding(client, mcp_token(ctx), repo, instance_id="inst-old")
    assert post_hook(client, hooks, session_end(repo, session_id="sess-old")).status_code == 200
    return run_id


def _hosted_run_resumed(ctx: AppContext, project_id: str, repo: str) -> str:
    run_id, attempt_id = hosted_attempt(ctx, project_id)
    client = loopback_client(ctx)
    hooks = hooks_token(ctx)
    body = session_start(repo, session_id="sess-hosted-old", source="resume")
    assert post_hook(client, hooks, body).status_code == 200
    assert _record_finding(client, mcp_token(ctx), repo, instance_id="inst-hosted-old") == run_id
    with ctx.session() as session:
        # Stands in for the worker's findings monitor, which ingests a hosted run's events.
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
    return run_id


def test_the_verifier_version_changed_with_the_observed_coverage_rule() -> None:
    assert VERIFIER_VERSION == "2"


def test_outdated_never_read_on_hook_read_runs_is_rechecked_and_launched_runs_are_left_alone(
    ctx: AppContext, repo_project: str
) -> None:
    project_id, repo = svc_project(ctx)
    observed_run = _observed_run_joined_mid_session(ctx, str(repo))
    observed_citation = _old_never_read(ctx, observed_run)
    hosted_run = _hosted_run_resumed(ctx, project_id, str(repo))
    hosted_citation = _old_never_read(ctx, hosted_run)
    launched_run = run_with(
        ctx, repo_project, [finding_record("F1", "src/greeter.py", 1, 1, "def greet():")]
    )
    launched_citation = _old_never_read(ctx, launched_run)

    rechecked = VerificationService(ctx).recheck_outdated_never_read()

    assert sorted(rechecked) == sorted([observed_run, hosted_run])
    for citation_id in (observed_citation, hosted_citation):
        latest = _latest(ctx, citation_id)
        assert (latest.verdict, latest.verifier_version) == ("read_not_observed", "2")
    launched = _latest(ctx, launched_citation)
    assert (launched.verdict, launched.verifier_version) == ("never_read", "1")
    # Nothing outdated is left, so a second pass (the next serve start) records nothing.
    assert VerificationService(ctx).recheck_outdated_never_read() == []


def test_serve_rechecks_outdated_verdicts_once_at_start(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def recheck(self: VerificationService) -> list[str]:
        calls.append("recheck")
        return []

    monkeypatch.setattr(VerificationService, "recheck_outdated_never_read", recheck)
    stop = asyncio.Event()
    stop.set()
    asyncio.run(ObservedSessionSweeper(ctx).run(stop))
    assert calls == ["recheck"]
