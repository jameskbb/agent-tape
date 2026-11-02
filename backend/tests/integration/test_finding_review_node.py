"""The ``finding_review`` workflow node: challenges an upstream ``agent_run`` node's
findings and, unlike a test that only checks the child run, drives the WORKFLOW RUN
ITSELF to a terminal state. This is the hard gate against a known trap: a child
run created with the wrong ``parent_relation`` would leave the workflow silently hanging
forever, and a test that only asserted the child run's own status would pass regardless.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from agenttape.application.context import AppContext
from agenttape.application.schemas.catalog import AgentProfileCreate
from agenttape.application.schemas.workflows import WorkflowCreate, WorkflowRunCreate
from agenttape.application.services.finding_review import FindingReviewService
from agenttape.application.services.profiles import AgentProfileService
from agenttape.application.services.runs import RunService
from agenttape.application.services.workflows import WorkflowService
from agenttape.infrastructure.db.models import Run, StepRun
from agenttape.workflows.nodes.agent_run import child_idempotency_key
from tests.conftest import demo_profile, drive
from tests.fixtures.review_injection import F_REF, F_UP, finding_event
from tests.integration.test_context_evidence import ScriptedAdapter, run_with


#: The demo fixture's GREETER_V1 return line (line 5).
def _target_agent_profile(ctx: AppContext, records: list[dict[str, Any]]) -> str:
    if not ctx.adapters.has("test_context"):
        ctx.adapters.register(ScriptedAdapter())
    return (
        AgentProfileService(ctx)
        .create(
            AgentProfileCreate(
                name=f"finding-review-node-target-{len(records)}",
                adapter_type="test_context",
                config={"records": records},
            )
        )
        .id
    )


def _definition(agent_profile_id: str, reviewer_profile_id: str, project_id: str) -> dict[str, Any]:
    return {
        "settings": {"project_id": project_id},
        "nodes": [
            {
                "key": "agent",
                "type": "agent_run",
                "config": {"agent_profile_id": agent_profile_id, "prompt": "Fix src/greeter.py"},
            },
            {
                "key": "review",
                "type": "finding_review",
                "config": {"target": "agent", "agent_profile_id": reviewer_profile_id},
            },
        ],
        "edges": [{"from": "agent", "to": "review"}],
    }


def _create_and_start(
    ctx: AppContext, name: str, agent_profile_id: str, reviewer_profile_id: str, project_id: str
) -> Any:
    workflow_id = (
        WorkflowService(ctx)
        .create(
            WorkflowCreate(
                name=name,
                definition=_definition(agent_profile_id, reviewer_profile_id, project_id),
            )
        )
        .id
    )
    return WorkflowService(ctx).start_run(workflow_id, WorkflowRunCreate(inputs={})).run


def _step(ctx: AppContext, run_id: str, node_key: str) -> StepRun:
    with ctx.session() as session:
        step = session.scalars(
            select(StepRun).where(StepRun.run_id == run_id, StepRun.node_key == node_key)
        ).first()
        assert step is not None, f"no step '{node_key}' on run {run_id}"
        session.expunge(step)
        return step


def test_finding_review_node_challenges_the_target_and_the_workflow_run_succeeds(
    ctx: AppContext, repo_project: str
) -> None:
    agent_profile_id = _target_agent_profile(ctx, [finding_event(F_UP), finding_event(F_REF)])
    reviewer_profile_id = demo_profile(ctx, "finding_review")
    workflow_run = _create_and_start(
        ctx, "Finding review node", agent_profile_id, reviewer_profile_id, repo_project
    )

    drive(ctx, workflow_run.id)

    settled = RunService(ctx).get_run(workflow_run.id)
    assert settled.status == "succeeded", settled.failure_message

    agent_step = _step(ctx, workflow_run.id, "agent")
    assert agent_step.child_run_id is not None
    target_run_id = agent_step.child_run_id

    review_step = _step(ctx, workflow_run.id, "review")
    assert review_step.child_run_id is not None
    with ctx.session() as session:
        review_run = session.get(Run, review_step.child_run_id)
        assert review_run is not None
        assert review_run.parent_relation == "workflow_node"
        assert review_run.parent_node_key == "review"
        assert review_run.source == "workflow"
        assert review_run.parent_run_id == workflow_run.id
        assert review_run.snapshot["finding_review"]["target_run_id"] == target_run_id

    challenges = FindingReviewService(ctx).challenges(target_run_id)
    assert len(challenges) == 1
    [challenge] = challenges
    assert challenge.trigger == "workflow_node"
    assert challenge.state == "completed"
    outcomes = {o.target_finding_key: o.outcome for o in challenge.outcomes}
    assert outcomes == {"F-UP": "upheld", "F-REF": "refuted"}
    assert challenge.judgment == "model judgment"
    assert challenge.counts_as_proof is False

    review_outputs = review_step.outputs or {}
    assert review_outputs["child_run_id"] == review_step.child_run_id
    assert review_outputs["reviewed"] == 2
    assert review_outputs["upheld"] == 1
    assert review_outputs["refuted"] == 1


def test_restarting_the_review_node_reuses_the_existing_child(
    ctx: AppContext, repo_project: str
) -> None:
    """Simulates a restarted node start directly against the service: a fresh call with the
    same ``(workflow_run_id, node_key, attempt)`` must find the existing child rather than
    rebuilding a request (whose prompt carries a fresh random nonce each time and would
    otherwise conflict with the stored idempotency fingerprint). Uses two ordinary,
    already-finished runs (never driven again) as stand-ins for the workflow run and its
    target, so the real engine never touches this idempotency key out from under the test.
    """
    fake_workflow_run_id = run_with(ctx, repo_project, [])
    target_run_id = run_with(ctx, repo_project, [finding_event(F_UP)])
    reviewer_profile_id = demo_profile(ctx, "finding_review")

    service = FindingReviewService(ctx)
    kwargs: dict[str, Any] = {
        "workflow_run_id": fake_workflow_run_id,
        "workflow_id": None,
        "workflow_version_id": None,
        "workflow_name": "Finding review node idempotency",
        "node_key": "review",
        "node_label": "Review",
        "attempt": 1,
        "target_run_id": target_run_id,
        "agent_profile_id": reviewer_profile_id,
    }
    child_id_1, created_1 = service.start_for_workflow_node(**kwargs)
    assert created_1 is True
    child_id_2, created_2 = service.start_for_workflow_node(**kwargs)
    assert created_2 is False
    assert child_id_1 == child_id_2

    key = child_idempotency_key(fake_workflow_run_id, "review", 1)
    with ctx.session() as session:
        matches = session.scalars(
            select(Run).where(Run.source == "workflow", Run.idempotency_key == key)
        ).all()
        assert len(matches) == 1


def test_a_target_with_no_findings_fails_the_review_node_not_the_workflow_silently(
    ctx: AppContext, repo_project: str
) -> None:
    agent_profile_id = _target_agent_profile(ctx, [])
    reviewer_profile_id = demo_profile(ctx, "finding_review")
    workflow_run = _create_and_start(
        ctx, "Finding review node no findings", agent_profile_id, reviewer_profile_id, repo_project
    )

    drive(ctx, workflow_run.id)

    settled = RunService(ctx).get_run(workflow_run.id)
    assert settled.status == "failed"

    review_step = _step(ctx, workflow_run.id, "review")
    assert review_step.status == "failed"
    assert review_step.child_run_id is None
    assert review_step.failure_message is not None
    assert "no findings" in review_step.failure_message.lower()
