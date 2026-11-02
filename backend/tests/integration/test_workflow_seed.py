"""The seeded "Safe Code Change" workflow runs for real, pauses, and passes its suite."""

from __future__ import annotations

from agenttape.application.context import AppContext
from agenttape.application.services.approvals import ApprovalService
from agenttape.application.services.demo import DemoService
from agenttape.application.services.runs import RunService
from agenttape.domain.enums import RunStatus
from tests.conftest import drive


def test_seeded_workflow_pauses_then_passes_its_evaluation_suite(ctx: AppContext) -> None:
    report = DemoService(ctx).seed()
    run_id = report.data["workflow_run_id"]
    assert DemoService(ctx).seed().data["workflow_run_id"] == run_id  # idempotent

    detail = drive(ctx, run_id, timeout=60)
    assert detail.run.status == RunStatus.WAITING_FOR_APPROVAL
    steps = {s.node_key: s for s in detail.steps}
    assert steps["prepare"].status == "succeeded" and steps["prepare"].outputs
    assert steps["baseline"].status == "succeeded"
    assert steps["implement"].status == "succeeded"
    assert steps["approve"].status == "waiting_for_approval"
    approval = ApprovalService(ctx).list_approvals(status=["pending"], run_id=run_id).items[0]
    assert approval.title.startswith("Approve the change")
    assert approval.requested_action["summary"]

    ApprovalService(ctx).approve(approval.id, reviewer="demo reviewer")
    detail = drive(ctx, run_id, timeout=60)
    assert detail.run.status == RunStatus.SUCCEEDED, detail.run.failure_message
    gate = {s.node_key: s for s in detail.steps}["evaluate"].outputs or {}
    assert gate["passed"] is True, gate
    assert gate["statuses"]["Token budget"] == "skipped"
    assert {e.name for e in detail.evaluations} >= {
        "Agent exited cleanly",
        "Small diff, no protected paths",
        "Change report produced",
    }
    assert any(a.name == "safe-code-change.json" for a in detail.artifacts)


def test_implicit_seeding_creates_no_runs(ctx: AppContext) -> None:
    DemoService(ctx).seed(include_runs=False)
    # Other tracks may seed finished runs; this seed adds no workflow run implicitly.
    assert RunService(ctx).list_runs(kind="workflow").total == 0
