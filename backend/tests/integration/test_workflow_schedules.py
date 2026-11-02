"""Schedules start workflow runs that the workflow executor runs end to end."""

from __future__ import annotations

from agenttape.application.context import AppContext
from agenttape.application.schemas.schedules import ScheduleCreate
from agenttape.application.schemas.workflows import WorkflowCreate
from agenttape.application.services.approvals import ApprovalService
from agenttape.application.services.schedules import ScheduleService, workflow_runs_available
from agenttape.application.services.workflows import WorkflowService
from agenttape.domain.enums import RunSource, RunStatus
from agenttape.infrastructure.db.models import WorkflowVersion
from tests.conftest import demo_profile, drive


def test_scheduled_workflow_run_uses_the_workflow_executor(ctx: AppContext) -> None:
    assert workflow_runs_available()
    profile = demo_profile(ctx)
    workflow = WorkflowService(ctx).create(
        WorkflowCreate(
            name="Scheduled change",
            definition={
                "inputs": [{"name": "goal", "default": "tidy the greeter"}],
                "nodes": [
                    {
                        "key": "brief",
                        "type": "transform",
                        "config": {"template": "Goal: {{ inputs.goal }}"},
                    },
                    {
                        "key": "agent",
                        "type": "agent_run",
                        "config": {
                            "agent_profile_id": profile,
                            "prompt": "{{ nodes.brief.outputs.text }}",
                        },
                    },
                    {"key": "approve", "type": "approval", "config": {"title": "Keep it?"}},
                ],
                "edges": [{"from": "brief", "to": "agent"}, {"from": "agent", "to": "approve"}],
            },
        )
    )
    schedule = ScheduleService(ctx).create(
        ScheduleCreate(
            name="Nightly workflow",
            kind="cron",
            cron_expression="0 2 * * *",
            timezone="UTC",
            target_type="workflow",
            workflow_id=workflow.id,
        )
    )
    result = ScheduleService(ctx).run_now(schedule.id)
    assert result.run_id, result.message
    detail = drive(ctx, result.run_id)
    assert detail.run.kind == "workflow" and detail.run.source == RunSource.SCHEDULE
    assert detail.run.status == RunStatus.WAITING_FOR_APPROVAL
    steps = {s.node_key: s for s in detail.steps}
    # Input defaults were applied for the schedule-created run.
    assert (steps["brief"].outputs or {})["text"] == "Goal: tidy the greeter"
    assert steps["agent"].status == "succeeded" and len(detail.children) == 1
    with ctx.session() as session:
        version = session.get(WorkflowVersion, detail.run.workflow_version_id)
        assert version is not None and version.locked_at is not None

    approval = ApprovalService(ctx).list_approvals(status=["pending"], run_id=result.run_id)
    ApprovalService(ctx).approve(approval.items[0].id, reviewer="me")
    assert drive(ctx, result.run_id).run.status == RunStatus.SUCCEEDED
