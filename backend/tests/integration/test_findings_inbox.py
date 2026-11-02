"""``GET /findings``: one cross-run read model listing every finding recorded across runs in
a window, flagged verdicts first, filtered by project, agent, verdict, kind, source, and
text.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from agenttape.application.context import AppContext
from agenttape.application.schemas.catalog import ProjectCreate
from agenttape.application.services.findings import FindingsService
from agenttape.application.services.projects import ProjectService
from agenttape.application.services.runs import RunService
from agenttape.domain.enums import RunSource
from agenttape.infrastructure.db.base import new_id
from agenttape.infrastructure.db.extra_models.findings import Finding
from agenttape.infrastructure.db.models import Event, Project, Run
from tests.integration.helpers import (
    GREETER_TEXT,
    finding_record,
    read_records,
    run_findings,
)
from tests.integration.test_context_evidence import git, run_with


def _direct_finding(
    ctx: AppContext, run_id: str, finding_id: str, text: str, *, ts_offset_days: int = 0
) -> None:
    """A declared-only finding (no links), inserted directly rather than through a driven
    run - used for the observed run's finding and for a finding recorded outside the
    window."""
    with ctx.session() as session:
        run = session.get(Run, run_id)
        assert run is not None
        ts = ctx.clock.now() - timedelta(days=ts_offset_days) if ts_offset_days else ctx.clock.now()
        next_seq = (
            session.scalar(select(func.max(Event.sequence)).where(Event.run_id == run_id)) or 0
        ) + 1
        event = Event(
            id=new_id(),
            run_id=run_id,
            sequence=next_seq,
            ts=ts,
            type="finding",
            source="test",
            severity="info",
            summary=finding_id,
            payload={"id": finding_id, "kind": "claim", "text": text},
        )
        session.add(event)
        session.flush()
        FindingsService.ingest_event(session, run, event, None)


def _build_window(ctx: AppContext, project_id: str) -> dict[str, str]:
    """Three runs in the window: an agent run whose finding's quote is not found (worst), an
    agent run whose finding's quote matches (best), and an observed run with a declared-only
    finding - plus a finding recorded 30 days ago, outside a 7-day window."""
    quote_ok = "def greet():\n    return 'hi'"
    run_a = run_with(
        ctx,
        project_id,
        [
            *read_records("toolu_a", "src/greeter.py", GREETER_TEXT),
            finding_record(
                "F1",
                "src/greeter.py",
                1,
                1,
                "def greet(name):",
                kind="claim",
                text="a whitespace-sensitive claim",
            ),
        ],
        prompt="run a",
    )
    run_b = run_with(
        ctx,
        project_id,
        [
            *read_records("toolu_b", "src/greeter.py", GREETER_TEXT),
            finding_record(
                "F1", "src/greeter.py", 1, 2, quote_ok, kind="recommendation", text="100% correct"
            ),
        ],
        prompt="run b",
    )

    with ctx.session() as session:
        project = session.get(Project, project_id)
        assert project is not None
        run, _attempt = RunService(ctx).create_observed_run(
            session,
            project=project,
            cwd=project.repo_path,
            source=RunSource.HOOK,
            source_ref="test-hook",
        )
        run_c = run.id
    _direct_finding(ctx, run_c, "F1", "an observed, declared-only claim")

    _direct_finding(ctx, run_a, "OLD", "a finding from outside the window", ts_offset_days=30)

    # Explicit, distinct recording times (a, then b, then c) so the order never depends on
    # the wall clock's resolution between the three runs.
    now = ctx.clock.now()
    with ctx.session() as session:
        for minutes_ago, run_id in ((3, run_a), (2, run_b), (1, run_c)):
            finding = session.scalars(
                select(Finding).where(Finding.run_id == run_id, Finding.finding_key == "F1")
            ).one()
            finding.created_at = now - timedelta(minutes=minutes_ago)

    return {"run_a": run_a, "run_b": run_b, "run_c": run_c}


def test_window_lists_flagged_verdicts_first_and_excludes_older_findings(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    ids = _build_window(ctx, repo_project)
    resp = client.get("/api/v1/findings", params={"days": 7})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 3
    run_ids = [item["run"]["id"] for item in body["items"]]
    assert run_ids == [ids["run_a"], ids["run_c"], ids["run_b"]]
    worst = [item["worst_verdict"] for item in body["items"]]
    assert worst == ["quote_not_found", "declared_only", "quote_matches"]
    keys = {item["key"] for item in body["items"]}
    assert "OLD" not in keys


def test_sort_newest_orders_by_created_at_descending(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    ids = _build_window(ctx, repo_project)
    resp = client.get("/api/v1/findings", params={"days": 7, "sort": "newest"})
    assert resp.status_code == 200, resp.text
    run_ids = [item["run"]["id"] for item in resp.json()["items"]]
    assert run_ids == [ids["run_c"], ids["run_b"], ids["run_a"]]


def test_verdict_and_kind_filters(ctx: AppContext, client: TestClient, repo_project: str) -> None:
    ids = _build_window(ctx, repo_project)

    resp = client.get("/api/v1/findings", params={"days": 7, "verdict": "quote_not_found"})
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["run"]["id"] == ids["run_a"]

    resp = client.get("/api/v1/findings", params={"days": 7, "verdict": "declared_only"})
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["run"]["id"] == ids["run_c"]

    resp = client.get("/api/v1/findings", params={"days": 7, "kind": "recommendation"})
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["run"]["id"] == ids["run_b"]

    assert (
        client.get("/api/v1/findings", params={"verdict": "not-a-real-verdict"}).status_code == 422
    )


def test_source_filter_launched_vs_observed(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    ids = _build_window(ctx, repo_project)

    resp = client.get("/api/v1/findings", params={"days": 7, "source": "launched"})
    body = resp.json()
    assert body["total"] == 2
    assert {item["run"]["id"] for item in body["items"]} == {ids["run_a"], ids["run_b"]}

    resp = client.get("/api/v1/findings", params={"days": 7, "source": "observed"})
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["run"]["id"] == ids["run_c"]


def test_project_and_agent_profile_filters(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    ids = _build_window(ctx, repo_project)

    other_repo = Path(ctx.settings.allowed_roots[0]) / "other-repo"
    (other_repo / "src").mkdir(parents=True)
    (other_repo / "src" / "x.py").write_text("x = 1\n")
    git(other_repo, "init", "--quiet", "--initial-branch=main")
    git(other_repo, "add", "-A")
    git(other_repo, "commit", "--quiet", "-m", "init")
    other_project = (
        ProjectService(ctx).create(ProjectCreate(name="OtherRepo", repo_path=str(other_repo))).id
    )
    run_with(
        ctx,
        other_project,
        [{"type": "finding", "payload": {"id": "F1", "text": "a finding in another project"}}],
        prompt="other project run",
    )

    resp = client.get("/api/v1/findings", params={"days": 7, "project_id": repo_project})
    body = resp.json()
    assert body["total"] == 3
    assert all(item["run"]["project_id"] == repo_project for item in body["items"])

    agent_profile_id = RunService(ctx).get_run(ids["run_a"]).agent_profile_id
    resp = client.get("/api/v1/findings", params={"days": 7, "agent_profile_id": agent_profile_id})
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["run"]["id"] == ids["run_a"]


def test_search_matches_case_insensitively_and_treats_percent_literally(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    ids = _build_window(ctx, repo_project)
    # A decoy in the window containing "100" without a literal "%": an unescaped search for
    # "100%" (LIKE wildcard) would wrongly match this too.
    _direct_finding(ctx, ids["run_c"], "F2", "100 units shipped, no percent here")

    resp = client.get("/api/v1/findings", params={"days": 7, "q": "WHITESPACE"})
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["run"]["id"] == ids["run_a"]

    resp = client.get("/api/v1/findings", params={"days": 7, "q": "100%"})
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["run"]["id"] == ids["run_b"]


def test_run_summary_and_tally_match_the_runs_own_findings_for_run(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    ids = _build_window(ctx, repo_project)
    run_out = RunService(ctx).get_run(ids["run_b"])
    own_findings = run_findings(ctx, ids["run_b"])
    [own_f1] = [f for f in own_findings if f["key"] == "F1"]

    resp = client.get("/api/v1/findings", params={"days": 7})
    [row] = [item for item in resp.json()["items"] if item["run"]["id"] == ids["run_b"]]

    assert row["run"]["title"] == run_out.title
    assert row["run"]["status"] == "succeeded"
    assert row["run"]["kind"] == "agent"
    assert row["run"]["agent_label"] == run_out.agent_profile_name
    assert row["run"]["project_name"] == "FindingsRepo"
    assert row["citations"] == len(own_f1["citations"])
    own_verdict_counts: dict[str, int] = {}
    for citation in own_f1["citations"]:
        key = citation["verdict"]["verdict"] if citation["verdict"] else "unchecked"
        own_verdict_counts[key] = own_verdict_counts.get(key, 0) + 1
    assert row["verdict_counts"] == own_verdict_counts


def test_pagination_and_days_bounds(ctx: AppContext, client: TestClient, repo_project: str) -> None:
    ids = _build_window(ctx, repo_project)
    resp = client.get("/api/v1/findings", params={"days": 7, "limit": 1, "offset": 1})
    body = resp.json()
    assert body["total"] == 3
    assert len(body["items"]) == 1
    assert body["items"][0]["run"]["id"] == ids["run_c"]

    assert client.get("/api/v1/findings", params={"days": 0}).status_code == 422
    assert client.get("/api/v1/findings", params={"days": 91}).status_code == 422
