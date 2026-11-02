"""On-demand "Challenge findings": POST /runs/{run_id}/challenge and
GET /runs/{run_id}/challenges, through the scripted, no-model Demo Agent reviewer."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agenttape.application.context import AppContext
from agenttape.application.schemas.catalog import AgentProfileCreate
from agenttape.application.schemas.runs import RunCreate
from agenttape.application.services.findings import FindingsService
from agenttape.application.services.profiles import AgentProfileService
from agenttape.application.services.runs import RunService
from agenttape.context_evidence.hooks import rescan_run
from agenttape.findings.review import extract_untrusted_block
from agenttape.infrastructure.db.extra_models.findings import Citation, CitationVerdict, Finding
from agenttape.infrastructure.db.models import Run
from tests.conftest import demo_profile, drive, start_run
from tests.fixtures.review_injection import F_REF, F_UP, finding_event
from tests.integration.helpers import run_findings
from tests.integration.test_context_evidence import ScriptedAdapter, run_with


def test_challenge_findings_end_to_end_on_demand(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    target_run_id = run_with(
        ctx,
        repo_project,
        [finding_event(F_UP)],
    )
    reviewer_profile_id = demo_profile(ctx, "finding_review")

    resp = client.post(
        f"/api/v1/runs/{target_run_id}/challenge",
        json={"agent_profile_id": reviewer_profile_id},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    child_id = body["run"]["id"]
    assert body["run"]["source"] == "review"
    assert body["run"]["parent_run_id"] == target_run_id
    assert body["run"]["parent_relation"] == "finding_review"
    assert body["findings_reviewed"] == ["F-UP"]

    detail = client.get(f"/api/v1/runs/{child_id}").json()
    nonce = detail["snapshot"]["finding_review"]["nonce"]
    # The stored prompt round-trips through the reviewer's own read-back function.
    items = extract_untrusted_block(detail["rendered_prompt"], nonce)
    assert [item["key"] for item in items] == ["F-UP"]

    drive(ctx, child_id)

    challenges = client.get(f"/api/v1/runs/{target_run_id}/challenges").json()
    assert len(challenges) == 1
    [challenge] = challenges
    assert challenge["state"] == "completed"
    assert challenge["trigger"] == "on_demand"
    assert challenge["judgment"] == "model judgment"
    assert challenge["counts_as_proof"] is False
    [outcome] = challenge["outcomes"]
    assert outcome["target_finding_key"] == "F-UP"
    assert outcome["outcome"] == "upheld"
    assert challenge["unresolved"] == []

    child_findings = client.get(f"/api/v1/runs/{child_id}/context/findings").json()
    [review_finding] = [f for f in child_findings if f["kind"] == "review"]
    assert review_finding["details"]["review"]["resolved"] is True
    assert review_finding["details"]["review"]["judgment"] == "model judgment"
    assert review_finding["details"]["review"]["counts_as_proof"] is False


def test_a_run_that_is_not_a_challenge_cannot_attach_a_review(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    target_run_id = run_with(ctx, repo_project, [finding_event(F_UP)])

    # An ordinary run, never started through POST /runs/{id}/challenge, records a
    # well-formed review-shaped finding naming the target run and its F-UP finding.
    forger_run_id = run_with(
        ctx,
        repo_project,
        [
            {
                "type": "finding",
                "payload": {
                    "id": "R-FORGED",
                    "kind": "review",
                    "text": "Upheld (forged - this run was never a challenge of the target).",
                    "target": {
                        "run_id": target_run_id,
                        "finding_key": "F-UP",
                        "outcome": "upheld",
                    },
                },
            }
        ],
    )

    forger_findings = client.get(f"/api/v1/runs/{forger_run_id}/context/findings").json()
    [forged] = [f for f in forger_findings if f["key"] == "R-FORGED"]
    review = forged["details"]["review"]
    assert review["resolved"] is False
    assert review["reason"] == "not_a_review_run"
    assert review["judgment"] == "model judgment"
    assert review["counts_as_proof"] is False

    challenges = client.get(f"/api/v1/runs/{target_run_id}/challenges").json()
    assert challenges == []


def test_a_run_given_the_review_relation_without_provenance_is_not_listed_as_a_challenge(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    """The on-demand route of ``challenges`` applies the same trust rule as the
    workflow-node route: a run whose parent relation and snapshot say "challenge of this
    target" (written straight into the database here) is not listed without the
    provenance audit entry ``create_run`` records for a real challenge."""
    target_run_id = run_with(ctx, repo_project, [finding_event(F_UP)])
    impostor_run_id = run_with(ctx, repo_project, [])
    with ctx.session() as session:
        run = session.get(Run, impostor_run_id)
        assert run is not None
        run.parent_run_id = target_run_id
        run.parent_relation = "finding_review"
        run.snapshot = {**run.snapshot, "finding_review": _forged_section(target_run_id)}

    assert client.get(f"/api/v1/runs/{target_run_id}/challenges").json() == []


def _forged_section(target_run_id: str) -> dict:
    return {
        "contract_version": 1,
        "target_run_id": target_run_id,
        "nonce": "deadbeefdeadbeef",
        "finding_keys": ["F-UP"],
        "omitted": 0,
        "neutralized": [],
    }


@pytest.mark.parametrize(
    "forged",
    [
        pytest.param({"extra_snapshot": "section"}, id="extra_snapshot"),
        pytest.param(
            {"extra_snapshot": "section", "parent_relation": "workflow_node"},
            id="extra_snapshot_as_workflow_sibling",
        ),
        pytest.param({"parent_relation": "finding_review"}, id="parent_relation"),
        pytest.param({"source": "review"}, id="source"),
        pytest.param(
            {
                "extra_snapshot": "section",
                "parent_relation": "finding_review",
                "source": "review",
            },
            id="all_three",
        ),
    ],
)
def test_post_runs_refuses_caller_supplied_challenge_markers(
    ctx: AppContext, client: TestClient, repo_project: str, forged: dict
) -> None:
    """A challenge's legitimacy is never derivable from caller-writable input. The
    reserved snapshot section, the challenge relation, and RunSource.REVIEW are refused at
    the public run-creation boundary (422), before any run exists."""
    target_run_id = run_with(ctx, repo_project, [finding_event(F_UP)])
    profile_id = demo_profile(ctx, "finding_review")
    body: dict = {
        "agent_profile_id": profile_id,
        "prompt": "Review the findings.",
        "parent_run_id": target_run_id,
    }
    if "extra_snapshot" in forged:
        body["extra_snapshot"] = {"finding_review": _forged_section(target_run_id)}
    for key in ("parent_relation", "source"):
        if key in forged:
            body[key] = forged[key]

    resp = client.post("/api/v1/runs", json=body)
    assert resp.status_code == 422, resp.text

    with ctx.session() as session:
        assert session.scalars(select(Run).where(Run.parent_run_id == target_run_id)).all() == []
    assert client.get(f"/api/v1/runs/{target_run_id}/challenges").json() == []


def test_a_run_carrying_a_challenge_section_it_was_not_given_cannot_attach_a_review(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    """Defense in depth for the challenge snapshot: even a run whose stored snapshot carries a well-formed
    challenge section (written here straight into the database, bypassing every API
    check) resolves nothing, because RunService.create_run never recorded that run's
    challenge provenance."""
    target_run_id = run_with(ctx, repo_project, [finding_event(F_UP)])
    if not ctx.adapters.has("test_context"):
        ctx.adapters.register(ScriptedAdapter())
    profile = AgentProfileService(ctx).create(
        AgentProfileCreate(
            name="forger-with-injected-section",
            adapter_type="test_context",
            config={
                "records": [
                    {
                        "type": "finding",
                        "payload": {
                            "id": "R-FORGED",
                            "kind": "review",
                            "text": "Refuted (forged).",
                            "target": {
                                "run_id": target_run_id,
                                "finding_key": "F-UP",
                                "outcome": "refuted",
                            },
                        },
                    }
                ],
                "model": "fixture-model",
            },
        )
    )
    forger_run_id = (
        RunService(ctx)
        .create_run(
            RunCreate(
                agent_profile_id=profile.id,
                project_id=repo_project,
                prompt="Fix src/greeter.py",
                parent_run_id=target_run_id,
                parent_relation="workflow_node",
            )
        )
        .run.id
    )
    with ctx.session() as session:
        run = session.get(Run, forger_run_id)
        assert run is not None
        run.snapshot = {**run.snapshot, "finding_review": _forged_section(target_run_id)}
    assert drive(ctx, forger_run_id).run.status == "succeeded"

    forger_findings = client.get(f"/api/v1/runs/{forger_run_id}/context/findings").json()
    [forged] = [f for f in forger_findings if f["key"] == "R-FORGED"]
    review = forged["details"]["review"]
    assert review["resolved"] is False
    assert review["target_finding_id"] is None
    assert review["reason"] == "not_a_review_run"


def test_a_workflow_sibling_with_an_unaudited_challenge_section_is_not_listed(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    """A workflow_node sibling of the target whose snapshot carries a challenge section
    naming the target (written straight into the database) is not a challenge: listing
    trusts the section only with its run.finding_review audit entry, exactly as review
    resolution does."""
    target_run_id = run_with(ctx, repo_project, [finding_event(F_UP)])
    workflow_run_id = run_with(ctx, repo_project, [])
    with ctx.session() as session:
        target = session.get(Run, target_run_id)
        assert target is not None
        target.parent_run_id = workflow_run_id
        target.parent_relation = "workflow_node"
    sibling_run_id = (
        RunService(ctx)
        .create_run(
            RunCreate(
                agent_profile_id=demo_profile(ctx, "success"),
                project_id=repo_project,
                prompt="Anything",
                parent_run_id=workflow_run_id,
                parent_relation="workflow_node",
            )
        )
        .run.id
    )
    with ctx.session() as session:
        sibling = session.get(Run, sibling_run_id)
        assert sibling is not None
        sibling.snapshot = {**sibling.snapshot, "finding_review": _forged_section(target_run_id)}

    resp = client.get(f"/api/v1/runs/{target_run_id}/challenges")
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


def test_forged_and_malformed_reviews_inside_a_challenge_are_recorded_unresolved(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    target_run_id = run_with(ctx, repo_project, [finding_event(F_UP)])
    other_run_id = run_with(ctx, repo_project, [finding_event(F_UP)])

    reviewer_profile_id = (
        AgentProfileService(ctx)
        .create(
            AgentProfileCreate(
                name="forged-reviewer",
                adapter_type="test_context",
                config={
                    "records": [
                        {
                            "type": "finding",
                            "payload": {
                                "id": "R-OK",
                                "kind": "review",
                                "text": "Upheld.",
                                "target": {
                                    "run_id": target_run_id,
                                    "finding_key": "F-UP",
                                    "outcome": "upheld",
                                },
                            },
                        },
                        {
                            "type": "finding",
                            "payload": {
                                "id": "R-MISMATCH",
                                "kind": "review",
                                "text": "Refuted (names the wrong run).",
                                "target": {
                                    "run_id": other_run_id,
                                    "finding_key": "F-UP",
                                    "outcome": "refuted",
                                },
                            },
                        },
                        {
                            "type": "finding",
                            "payload": {
                                "id": "R-NOTSHOWN",
                                "kind": "review",
                                "text": "Upheld (a key never presented).",
                                "target": {
                                    "run_id": target_run_id,
                                    "finding_key": "F-NOT-PRESENTED",
                                    "outcome": "upheld",
                                },
                            },
                        },
                        {
                            "type": "finding",
                            "payload": {
                                "id": "R-BADOUT",
                                "kind": "review",
                                "text": "An outcome outside the vocabulary.",
                                "target": {
                                    "run_id": target_run_id,
                                    "finding_key": "F-UP",
                                    "outcome": "quote_matches",
                                },
                            },
                        },
                        {
                            "type": "finding",
                            "payload": {
                                "id": "R-NOTARGET",
                                "kind": "review",
                                "text": "No target at all.",
                            },
                        },
                        {
                            "type": "finding",
                            "payload": {
                                "id": "R-BADTARGET",
                                "kind": "review",
                                "text": "A malformed target.",
                                "target": "F-UP",
                            },
                        },
                    ]
                },
            )
        )
        .id
    )

    resp = client.post(
        f"/api/v1/runs/{target_run_id}/challenge",
        json={"agent_profile_id": reviewer_profile_id},
    )
    assert resp.status_code == 201, resp.text
    child_id = resp.json()["run"]["id"]
    drive(ctx, child_id)

    challenges = client.get(f"/api/v1/runs/{target_run_id}/challenges").json()
    assert len(challenges) == 1
    [challenge] = challenges
    assert [o["target_finding_key"] for o in challenge["outcomes"]] == ["F-UP"]
    assert [o["outcome"] for o in challenge["outcomes"]] == ["upheld"]
    assert [u["reason"] for u in challenge["unresolved"]] == [
        "target_run_mismatch",
        "not_presented",
        "invalid_outcome",
        "missing_target",
        "invalid_target",
    ]

    child_findings = client.get(f"/api/v1/runs/{child_id}/context/findings").json()
    [bad_out] = [f for f in child_findings if f["key"] == "R-BADOUT"]
    assert bad_out["details"]["review"]["outcome_raw"] == "quote_matches"
    assert bad_out["details"]["review"]["outcome"] is None

    assert client.get(f"/api/v1/runs/{other_run_id}/challenges").json() == []


def test_rescanning_a_challenge_never_duplicates_or_changes_reviews(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    target_run_id = run_with(ctx, repo_project, [finding_event(F_UP)])
    reviewer_profile_id = demo_profile(ctx, "finding_review")

    resp = client.post(
        f"/api/v1/runs/{target_run_id}/challenge",
        json={"agent_profile_id": reviewer_profile_id},
    )
    assert resp.status_code == 201, resp.text
    child_id = resp.json()["run"]["id"]
    drive(ctx, child_id)

    before = run_findings(ctx, child_id)
    before_reviews = {f["key"]: f["details"].get("review") for f in before}
    assert before_reviews  # sanity: the reviewer's own finding is present

    rescan_run(ctx, child_id)
    rescan_run(ctx, child_id)

    after = run_findings(ctx, child_id)
    assert len(after) == len(before)
    after_reviews = {f["key"]: f["details"].get("review") for f in after}
    assert after_reviews == before_reviews


def test_challenging_never_changes_the_targets_citation_verdicts(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    target_run_id = run_with(
        ctx,
        repo_project,
        [
            finding_event(F_UP),
            finding_event(F_REF),
        ],
    )
    findings_service = FindingsService(ctx)
    findings_before_seed = run_findings(ctx, target_run_id)
    f_up_citation_id = next(
        c["id"] for f in findings_before_seed if f["key"] == "F-UP" for c in f["citations"]
    )
    findings_service.record_verdict(f_up_citation_id, verdict="quote_matches", method="seeded")

    snapshot_before = run_findings(ctx, target_run_id)
    latest_before = {f["key"]: [c["verdict"] for c in f["citations"]] for f in snapshot_before}
    target_citation_ids = [c["id"] for f in snapshot_before for c in f["citations"]]
    with ctx.session() as session:
        verdicts_before = {
            (v.id, v.citation_id, v.verdict, v.method)
            for v in session.scalars(
                select(CitationVerdict).where(CitationVerdict.citation_id.in_(target_citation_ids))
            )
        }

    reviewer_profile_id = demo_profile(ctx, "finding_review")
    resp = client.post(
        f"/api/v1/runs/{target_run_id}/challenge",
        json={"agent_profile_id": reviewer_profile_id},
    )
    assert resp.status_code == 201, resp.text
    child_id = resp.json()["run"]["id"]
    drive(ctx, child_id)

    snapshot_after = run_findings(ctx, target_run_id)
    latest_after = {f["key"]: [c["verdict"] for c in f["citations"]] for f in snapshot_after}
    assert latest_after == latest_before
    with ctx.session() as session:
        verdicts_after = {
            (v.id, v.citation_id, v.verdict, v.method)
            for v in session.scalars(
                select(CitationVerdict).where(CitationVerdict.citation_id.in_(target_citation_ids))
            )
        }
    assert verdicts_after == verdicts_before

    with ctx.session() as session:
        review_finding_ids = [
            row.id
            for row in session.scalars(
                select(Finding).where(Finding.run_id == child_id, Finding.kind == "review")
            )
        ]
        review_citations = list(
            session.scalars(select(Citation).where(Citation.finding_id.in_(review_finding_ids)))
        )
        assert review_citations  # sanity: the reviewer recorded its own citations
        for citation in review_citations:
            owner = session.get(Finding, citation.finding_id)
            assert owner is not None and owner.run_id == child_id

    challenges = client.get(f"/api/v1/runs/{target_run_id}/challenges").json()
    [challenge] = challenges
    outcomes = {o["target_finding_key"]: o["outcome"] for o in challenge["outcomes"]}
    assert outcomes == {"F-UP": "upheld", "F-REF": "refuted"}


def test_challenge_start_validates_run_profile_readiness_and_findings(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    reviewer_profile_id = demo_profile(ctx, "finding_review")

    resp = client.post(
        "/api/v1/runs/does-not-exist/challenge",
        json={"agent_profile_id": reviewer_profile_id},
    )
    assert resp.status_code == 404

    target_run_id = run_with(ctx, repo_project, [finding_event(F_UP)])
    resp = client.post(
        f"/api/v1/runs/{target_run_id}/challenge",
        json={"agent_profile_id": "does-not-exist"},
    )
    assert resp.status_code == 404

    queued_run_id = start_run(ctx, reviewer_profile_id)
    resp = client.post(
        f"/api/v1/runs/{queued_run_id}/challenge",
        json={"agent_profile_id": reviewer_profile_id},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "run_not_finished"

    empty_run_id = run_with(ctx, repo_project, [])
    resp = client.post(
        f"/api/v1/runs/{empty_run_id}/challenge",
        json={"agent_profile_id": reviewer_profile_id},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"
    assert client.get(f"/api/v1/runs/{empty_run_id}/challenges").json() == []


def test_a_challenge_whose_reviewer_fails_shows_did_not_complete_never_refuted(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    target_run_id = run_with(ctx, repo_project, [finding_event(F_UP)])
    failing_reviewer_id = demo_profile(ctx, "fail")

    resp = client.post(
        f"/api/v1/runs/{target_run_id}/challenge",
        json={"agent_profile_id": failing_reviewer_id},
    )
    assert resp.status_code == 201, resp.text
    child_id = resp.json()["run"]["id"]
    drive(ctx, child_id)

    challenges = client.get(f"/api/v1/runs/{target_run_id}/challenges").json()
    [challenge] = challenges
    assert challenge["state"] == "did_not_complete"
    assert challenge["outcomes"] == []
    assert all(o["outcome"] != "refuted" for c in challenges for o in c["outcomes"])
    detail = client.get(f"/api/v1/runs/{child_id}").json()
    assert detail["run"]["status"] in ("failed", "timed_out", "cancelled", "skipped")


def test_a_challenge_not_yet_driven_shows_pending(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    target_run_id = run_with(ctx, repo_project, [finding_event(F_UP)])
    reviewer_profile_id = demo_profile(ctx, "finding_review")

    resp = client.post(
        f"/api/v1/runs/{target_run_id}/challenge",
        json={"agent_profile_id": reviewer_profile_id},
    )
    assert resp.status_code == 201, resp.text

    challenges = client.get(f"/api/v1/runs/{target_run_id}/challenges").json()
    [challenge] = challenges
    assert challenge["state"] == "pending"


def test_a_completed_challenge_shows_usage_cost_as_recorded_and_appears_in_lineage(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    target_run_id = run_with(ctx, repo_project, [finding_event(F_UP)])
    reviewer_profile_id = demo_profile(ctx, "finding_review")

    resp = client.post(
        f"/api/v1/runs/{target_run_id}/challenge",
        json={"agent_profile_id": reviewer_profile_id},
    )
    assert resp.status_code == 201, resp.text
    child_id = resp.json()["run"]["id"]
    drive(ctx, child_id)

    challenges = client.get(f"/api/v1/runs/{target_run_id}/challenges").json()
    [challenge] = challenges
    assert challenge["run"]["usage"]["simulated"] is True

    child_detail = client.get(f"/api/v1/runs/{child_id}").json()
    assert challenge["run"]["cost"] is None
    assert child_detail["run"]["cost"] is None

    lineage = client.get(f"/api/v1/runs/{target_run_id}/lineage").json()
    children = {c["id"]: c["relation"] for c in lineage["children"]}
    assert children[child_id] == "finding_review"
