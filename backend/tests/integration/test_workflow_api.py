"""HTTP API for workflows, evaluation definitions/suites, approvals, and the safety service."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from agenttape.application.context import AppContext
from agenttape.application.services.approvals import ApprovalService
from agenttape.application.services.safety import SafetyApprovals, commit_action
from agenttape.domain.errors import PolicyViolationError
from agenttape.domain.policy import ApprovalType
from tests.conftest import demo_profile, drive, start_run


def small_definition(profile_id: str) -> dict[str, Any]:
    return {
        "inputs": [{"name": "who", "default": "world"}],
        "nodes": [
            {"key": "hello", "type": "transform", "config": {"template": "hello {{ inputs.who }}"}},
            {
                "key": "agent",
                "type": "agent_run",
                "config": {
                    "agent_profile_id": profile_id,
                    "prompt": "{{ nodes.hello.outputs.text }}",
                },
            },
            {"key": "ok", "type": "approval", "config": {"title": "OK?"}},
        ],
        "edges": [{"from": "hello", "to": "agent"}, {"from": "agent", "to": "ok"}],
    }


def test_invalid_definitions_are_rejected_before_saving(
    client: TestClient, ctx: AppContext
) -> None:
    cyclic = {
        "nodes": [
            {"key": "a", "type": "transform", "config": {"values": {}}},
            {"key": "b", "type": "transform", "config": {"values": {}}},
        ],
        "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "a"}],
    }
    check = client.post("/api/v1/workflows/validate", json={"definition": cyclic}).json()
    assert check["valid"] is False and "cycle" in check["issues"][0]["message"]
    resp = client.post("/api/v1/workflows", json={"name": "loop", "definition": cyclic})
    assert resp.status_code == 422
    assert "cycle" in str(resp.json()["error"]["details"])
    missing = {
        "nodes": [
            {"key": "a", "type": "agent_run", "config": {"agent_profile_id": "nope", "prompt": "x"}}
        ]
    }
    resp = client.post("/api/v1/workflows", json={"name": "ghost", "definition": missing})
    assert resp.status_code == 422 and "does not exist" in str(resp.json())


def test_versions_are_immutable_and_runs_go_through_the_engine(
    client: TestClient, ctx: AppContext
) -> None:
    profile = demo_profile(ctx)
    created = client.post(
        "/api/v1/workflows", json={"name": "Hello", "definition": small_definition(profile)}
    )
    assert created.status_code == 201, created.text
    wf = created.json()
    assert wf["latest_version"] == 1 and wf["node_count"] == 3
    same = client.post(
        f"/api/v1/workflows/{wf['id']}/versions", json={"definition": small_definition(profile)}
    )
    assert same.status_code == 409
    changed = small_definition(profile)
    changed["settings"] = {"max_parallel": 2}
    v2 = client.post(f"/api/v1/workflows/{wf['id']}/versions", json={"definition": changed})
    assert v2.status_code == 201 and v2.json()["version"] == 2

    start = client.post(
        f"/api/v1/workflows/{wf['id']}/runs",
        json={"inputs": {"who": "tests"}},
        headers={"Idempotency-Key": "wf-key-1"},
    )
    assert start.status_code == 201
    run = start.json()["run"]
    assert run["kind"] == "workflow" and run["workflow_version_id"] == v2.json()["id"]
    again = client.post(
        f"/api/v1/workflows/{wf['id']}/runs",
        json={"inputs": {"who": "tests"}},
        headers={"Idempotency-Key": "wf-key-1"},
    )
    assert again.status_code == 200 and again.json()["run"]["id"] == run["id"]
    # Same key, different request: a conflict, not a silently reused run.
    different = client.post(
        f"/api/v1/workflows/{wf['id']}/runs", json={}, headers={"Idempotency-Key": "wf-key-1"}
    )
    assert different.status_code == 409
    assert different.json()["error"]["code"] == "idempotency_key_conflict"

    versions = client.get(f"/api/v1/workflows/{wf['id']}/versions").json()
    assert versions[0]["locked_at"] is not None and versions[1]["locked_at"] is None

    drive(ctx, run["id"])
    detail = client.get(f"/api/v1/runs/{run['id']}").json()
    assert detail["run"]["status"] == "waiting_for_approval"
    assert [s["node_key"] for s in detail["steps"]] == ["hello", "agent", "ok"]
    assert detail["steps"][0]["outputs"] == {"text": "hello tests"}
    assert detail["snapshot"]["workflow"]["version"] == 2

    inbox = client.get("/api/v1/approvals", params={"status": "pending"}).json()
    approval = next(a for a in inbox["items"] if a["run_id"] == run["id"])
    assert approval["resume_handler"] == "workflow_node" and approval["node_key"] == "ok"
    decided = client.post(
        f"/api/v1/approvals/{approval['id']}/approve", json={"note": "fine"}
    ).json()
    assert decided["status"] == "approved" and decided["reviewer"] == "local user"
    assert client.post(f"/api/v1/approvals/{approval['id']}/reject", json={}).status_code == 409
    drive(ctx, run["id"])
    assert client.get(f"/api/v1/runs/{run['id']}").json()["run"]["status"] == "succeeded"

    listed = client.get("/api/v1/workflows").json()
    assert listed["items"][0]["last_run_status"] == "succeeded"
    types = client.get("/api/v1/workflow-node-types").json()
    assert {t["type"] for t in types} == {
        "agent_run",
        "command",
        "transform",
        "evaluation_gate",
        "approval",
        "condition",
        "collect",
        "finding_review",
    }


def test_a_finding_review_node_naming_an_unknown_agent_profile_is_refused(
    client: TestClient,
) -> None:
    definition = {
        "nodes": [
            {
                "key": "agent",
                "type": "agent_run",
                "config": {"agent_profile_id": "nope", "prompt": "x"},
            },
            {
                "key": "review",
                "type": "finding_review",
                "config": {"target": "agent", "agent_profile_id": "also-nope"},
            },
        ],
        "edges": [{"from": "agent", "to": "review"}],
    }
    check = client.post("/api/v1/workflows/validate", json={"definition": definition}).json()
    assert check["valid"] is False
    messages = [i["message"] for i in check["issues"]]
    assert any("Agent profile 'also-nope' does not exist" in m for m in messages), messages


def test_evaluation_definitions_suites_rerun_human_and_compare(
    client: TestClient, ctx: AppContext
) -> None:
    profile = demo_profile(ctx)
    run_a = start_run(ctx, profile, evaluators=[])
    run_b = start_run(ctx, demo_profile(ctx, "fail", name="failing"), evaluators=[])
    drive(ctx, run_a)
    drive(ctx, run_b)

    bad = client.post(
        "/api/v1/evaluation-definitions",
        json={"name": "bad", "evaluator_type": "regex", "config": {"pattern": "("}},
    )
    assert bad.status_code == 422
    regex = client.post(
        "/api/v1/evaluation-definitions",
        json={
            "name": "Mentions greet",
            "evaluator_type": "regex",
            "config": {"pattern": "greet", "source": "all"},
            "required": True,
        },
    ).json()
    duration = client.post(
        "/api/v1/evaluation-definitions",
        json={"name": "Fast", "evaluator_type": "duration", "config": {"max_seconds": 600}},
    ).json()
    v2 = client.post(
        f"/api/v1/evaluation-definitions/{duration['id']}/versions",
        json={"config": {"max_seconds": 300}},
    )
    assert v2.status_code == 201 and v2.json()["version"] == 2
    suite = client.post(
        "/api/v1/evaluation-suites",
        json={
            "name": "Basics",
            "members": [
                {"definition_id": regex["id"]},
                {"definition_id": duration["id"], "version": 1, "required": True},
            ],
        },
    )
    assert suite.status_code == 201
    suite_id = suite.json()["id"]

    for run_id in (run_a, run_b):
        result = client.post(f"/api/v1/runs/{run_id}/evaluations/run", json={"suite_id": suite_id})
        assert result.status_code == 200, result.text
        assert len(result.json()["results"]) == 2
    versions = client.get(f"/api/v1/evaluation-definitions/{duration['id']}/versions").json()
    assert next(v for v in versions if v["version"] == 1)["locked_at"] is not None

    rerun = client.post(f"/api/v1/runs/{run_a}/evaluations/rerun", json={}).json()
    assert {r["name"] for r in rerun["results"]} == {"Mentions greet", "Fast"}

    human = client.post(
        f"/api/v1/runs/{run_a}/evaluations/human",
        json={"score": 4, "max_score": 5, "pass_threshold": 3, "notes": "Clean change"},
    )
    assert human.status_code == 201
    assert human.json()["status"] == "pass" and human.json()["score"] == pytest.approx(0.8)

    compare = client.get(
        "/api/v1/evaluations/compare", params=[("run_id", run_a), ("run_id", run_b)]
    ).json()
    names = {row["name"]: row for row in compare["rows"]}
    assert set(names) == {"Mentions greet", "Fast", "Human review"}
    assert run_b not in names["Human review"]["cells"] and names["Human review"]["differs"]
    types = {t["type"]: t for t in client.get("/api/v1/evaluator-types").json()}
    assert types["agent_judge"]["available"] is False
    assert "disabled" in types["agent_judge"]["unavailable_reason"]


def test_policy_endpoint(client: TestClient) -> None:
    policy = client.get("/api/v1/policy").json()
    assert {p["type"] for p in policy["approval_required"]} >= {"commit", "protected_path"}
    assert any(d["action"] == "automatic_push" for d in policy["hard_disallowed"])
    verdict = client.post("/api/v1/policy/check-command", json={"argv": ["git", "push"]}).json()
    assert verdict["verdict"] == "deny"


def test_safety_approvals_commit_flow(ctx: AppContext) -> None:
    run_id = start_run(ctx, demo_profile(ctx))
    safety = SafetyApprovals(ctx)
    with ctx.session() as session:
        approval = safety.request_commit(
            session,
            run_id=run_id,
            repo_path="/repo",
            branch="agenttape/x",
            message="Fix greeting",
            files=["src/a.py"],
        )
        approval_id = approval.id
        assert approval.type == "commit" and approval.risk_level == "high"
    action = commit_action("/repo", "agenttape/x", "Fix greeting", ["src/a.py"])
    with ctx.session() as session, pytest.raises(PolicyViolationError):
        safety.require_approved(session, approval_id, ApprovalType.GIT_COMMIT, action=action)
    ApprovalService(ctx).approve(approval_id, reviewer="me")
    with ctx.session() as session:
        assert safety.require_approved(session, approval_id, ApprovalType.GIT_COMMIT, action=action)
        other = commit_action("/repo", "agenttape/x", "Something else", ["src/a.py"])
        with pytest.raises(PolicyViolationError):
            safety.require_approved(session, approval_id, ApprovalType.GIT_COMMIT, action=other)
        with pytest.raises(PolicyViolationError):
            safety.request_elevated_command(session, argv=["git", "push"], reason="x")
        assert (
            safety.request_protected_path_write(
                session, paths=["src/a.py"], patterns=[".github/**"]
            )
            is None
        )
        protected = safety.request_protected_path_write(
            session, paths=[".github/ci.yml"], patterns=[".github/**"], run_id=run_id
        )
        assert protected is not None and protected.type == "protected_path"
        budget = safety.request_budget_increase(
            session, run_id=run_id, current={"max_cost": 1}, proposed={"max_cost": 2}, reason="more"
        )
        assert budget is not None and budget.type == "budget_increase"
