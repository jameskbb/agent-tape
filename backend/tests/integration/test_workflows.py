"""Workflow engine: parallel dependencies, approval pause, restart, resume, cancellation."""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import pytest
from sqlalchemy import select

from agenttape.application.context import AppContext
from agenttape.application.schemas.workflows import WorkflowCreate, WorkflowRunCreate
from agenttape.application.services.approvals import ApprovalService
from agenttape.application.services.runs import RunService
from agenttape.application.services.workflows import WorkflowService
from agenttape.bootstrap import build_context
from agenttape.config import Settings
from agenttape.domain.enums import RunStatus
from agenttape.infrastructure.db.models import (
    ApprovalRequest,
    AuditEntry,
    Event,
    QueueItem,
    Run,
    StepRun,
)
from agenttape.workflows.maintenance import sweep_workflows
from tests.conftest import demo_profile, drive

STARTED = re.compile(r"^Node '[^']+' started( \(attempt \d+ of \d+\))?$")


def safe_change_definition(profile_id: str, **approval: Any) -> dict[str, Any]:
    """prep -> (agent_a || agent_b) -> approve -> validate -> gate -> collect."""
    return {
        "inputs": [{"name": "goal", "type": "string", "default": "tidy up"}],
        "settings": {"max_parallel": 4},
        "nodes": [
            {
                "key": "prep",
                "type": "command",
                "name": "Prepare",
                "config": {"argv": ["echo", "prepared {{ inputs.goal }}"]},
            },
            {
                "key": "agent_a",
                "type": "agent_run",
                "name": "Agent A",
                "config": {
                    "agent_profile_id": profile_id,
                    "prompt": "Work on: {{ nodes.prep.outputs.stdout }}",
                },
            },
            {
                "key": "agent_b",
                "type": "agent_run",
                "name": "Agent B",
                "config": {"agent_profile_id": profile_id, "prompt": "Review the change."},
            },
            {
                "key": "approve",
                "type": "approval",
                "name": "Approve the change",
                "config": {"title": "Ship {{ inputs.goal }}?", **approval},
            },
            {
                "key": "validate",
                "type": "command",
                "name": "Validate",
                "config": {"argv": ["echo", "{{ nodes.agent_a.outputs.summary }}"]},
            },
            {
                "key": "gate",
                "type": "evaluation_gate",
                "name": "Evaluate",
                "config": {
                    "target": "agent_a",
                    "evaluators": [
                        {"type": "exit_code", "name": "Agent exit code"},
                        {
                            "type": "usage_cost",
                            "name": "Token budget",
                            "required": False,
                            "config": {"max_total_tokens": 10},
                        },
                    ],
                },
            },
            {"key": "collect", "type": "collect", "name": "Collect"},
        ],
        "edges": [
            {"from": "prep", "to": "agent_a"},
            {"from": "prep", "to": "agent_b"},
            {"from": "agent_a", "to": "approve"},
            {"from": "agent_b", "to": "approve"},
            {"from": "agent_a", "to": "validate"},
            {"from": "approve", "to": "validate"},
            {"from": "validate", "to": "gate"},
            {"from": "agent_a", "to": "gate"},
            {"from": "gate", "to": "collect"},
        ],
    }


def create_workflow(ctx: AppContext, definition: dict[str, Any], name: str = "Safe change") -> str:
    return WorkflowService(ctx).create(WorkflowCreate(name=name, definition=definition)).id


def start(ctx: AppContext, workflow_id: str, **inputs: Any) -> str:
    return WorkflowService(ctx).start_run(workflow_id, WorkflowRunCreate(inputs=inputs)).run.id


def steps(ctx: AppContext, run_id: str) -> dict[str, StepRun]:
    with ctx.session() as session:
        rows = session.scalars(select(StepRun).where(StepRun.run_id == run_id)).all()
        session.expunge_all()
        return {s.node_key: s for s in rows}


def children(ctx: AppContext, run_id: str) -> list[Run]:
    with ctx.session() as session:
        rows = session.scalars(select(Run).where(Run.parent_run_id == run_id)).all()
        session.expunge_all()
        return list(rows)


def started_events(ctx: AppContext, run_id: str) -> dict[str, int]:
    with ctx.session() as session:
        rows = session.scalars(
            select(Event).where(Event.run_id == run_id, Event.type == "step")
        ).all()
        counts: dict[str, int] = {}
        for e in rows:
            if STARTED.match(e.summary) and e.node_key:
                counts[e.node_key] = counts.get(e.node_key, 0) + 1
        return counts


def pending_approval(ctx: AppContext, run_id: str) -> str:
    page = ApprovalService(ctx).list_approvals(status=["pending"], run_id=run_id)
    assert page.total == 1, page
    return page.items[0].id


def test_pause_restart_and_resume_without_rerunning_completed_nodes(settings: Settings) -> None:
    ctx = build_context(settings, migrate=True)
    profile = demo_profile(ctx)
    workflow_id = create_workflow(ctx, safe_change_definition(profile))
    run_id = start(ctx, workflow_id, goal="the greeter")

    detail = drive(ctx, run_id, worker_id="worker-before-restart")
    assert detail.run.status == RunStatus.WAITING_FOR_APPROVAL
    before = steps(ctx, run_id)
    assert {k: s.status for k, s in before.items()} == {
        "prep": "succeeded",
        "agent_a": "succeeded",
        "agent_b": "succeeded",
        "approve": "waiting_for_approval",
        "validate": "queued",
        "gate": "queued",
        "collect": "queued",
    }
    assert before["prep"].outputs["stdout"] == "prepared the greeter"
    kids = children(ctx, run_id)
    assert len(kids) == 2
    # Typed outputs flowed into a later node's input: the child got prep's stdout.
    agent_a = next(k for k in kids if k.parent_node_key == "agent_a")
    assert "prepared the greeter" in (agent_a.rendered_prompt or "")
    approval_id = pending_approval(ctx, run_id)
    ctx.close()

    # ---- simulated process restart: a brand-new context and worker identity ----
    ctx2 = build_context(settings, migrate=True)
    try:
        # Nothing to do while the approval is pending: the sweep leaves the run alone.
        assert sweep_workflows(ctx2)["woken"] == []
        with ctx2.session() as session:
            items_before = set(
                session.scalars(select(QueueItem.id).where(QueueItem.run_id == run_id))
            )
        ApprovalService(ctx2).approve(approval_id, reviewer="reviewer", note="looks good")
        detail = drive(ctx2, run_id, worker_id="worker-after-restart")
        assert detail.run.status == RunStatus.SUCCEEDED, detail.run.failure_message
        after = steps(ctx2, run_id)
        assert all(s.status == "succeeded" for s in after.values())
        # Completed nodes were not rerun: one attempt each, one start event each, and the
        # two agent children were not duplicated.
        assert {k: s.attempt_count for k, s in after.items()} == dict.fromkeys(after, 1)
        assert started_events(ctx2, run_id) == dict.fromkeys(after, 1)
        assert len(children(ctx2, run_id)) == 2
        assert after["approve"].outputs["reviewer"] == "reviewer"
        gate = after["gate"].outputs
        assert gate["passed"] is True
        # Demo usage is simulated, so the usage evaluator honestly skips.
        assert gate["statuses"]["Token budget"] == "skipped"
        assert detail.outputs is not None
        assert detail.outputs["result"]["collected"]["gate"]["status"] == "succeeded"
        with ctx2.session() as session:
            audits = session.scalars(
                select(AuditEntry.action).where(AuditEntry.resource_id == approval_id)
            ).all()
        assert "approval.approved" in audits
        # The resume was executed by the new worker identity, not the one that paused.
        with ctx2.session() as session:
            resumed = session.scalars(
                select(QueueItem).where(
                    QueueItem.run_id == run_id, QueueItem.id.not_in(items_before)
                )
            ).all()
        assert resumed, "the approval did not re-queue the workflow run"
        assert {i.lease_owner for i in resumed} == {"worker-after-restart"}
    finally:
        ctx2.close()


def test_parallel_children_run_concurrently(ctx: AppContext) -> None:
    from agenttape.worker.worker import Worker

    profile = demo_profile(ctx)
    definition = safe_change_definition(profile)
    workflow_id = create_workflow(ctx, definition)
    run_id = start(ctx, workflow_id)

    async def run_worker() -> None:
        worker = Worker(ctx, worker_id="parallel-worker", concurrency=4, poll_interval=0.02)
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run(stop))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            status = RunService(ctx).get_run(run_id).status
            if status == RunStatus.WAITING_FOR_APPROVAL:
                break
            await asyncio.sleep(0.05)
        stop.set()
        await task

    asyncio.run(run_worker())
    kids = children(ctx, run_id)
    assert len(kids) == 2
    a, b = sorted(kids, key=lambda r: r.started_at or r.created_at)
    # Both children were created by the same tick and their executions overlapped.
    assert a.started_at is not None and b.started_at is not None
    assert a.finished_at is not None
    assert b.started_at < a.finished_at


def test_rejection_fails_the_workflow_and_skips_the_rest(ctx: AppContext) -> None:
    profile = demo_profile(ctx)
    run_id = start(ctx, create_workflow(ctx, safe_change_definition(profile)))
    drive(ctx, run_id)
    ApprovalService(ctx).reject(pending_approval(ctx, run_id), reviewer="me", note="not today")
    detail = drive(ctx, run_id)
    assert detail.run.status == RunStatus.FAILED
    assert detail.run.failure_category == "approval_rejected"
    s = steps(ctx, run_id)
    assert s["approve"].status == "failed"
    assert {s["validate"].status, s["gate"].status, s["collect"].status} == {"skipped"}


def test_expiry_fails_the_workflow(ctx: AppContext) -> None:
    profile = demo_profile(ctx)
    definition = safe_change_definition(profile, expires_in_seconds=1)
    run_id = start(ctx, create_workflow(ctx, definition))
    drive(ctx, run_id)
    approval_id = pending_approval(ctx, run_id)
    time.sleep(1.2)
    report = sweep_workflows(ctx)
    assert report["expired_approvals"] == 1
    detail = drive(ctx, run_id)
    assert detail.run.status == RunStatus.FAILED
    assert detail.run.failure_category == "approval_expired"
    with ctx.session() as session:
        approval = session.get(ApprovalRequest, approval_id)
        assert approval is not None and approval.status == "expired"


def test_cancel_propagates_to_children_and_approvals(ctx: AppContext) -> None:
    profile = demo_profile(ctx)
    run_id = start(ctx, create_workflow(ctx, safe_change_definition(profile)))
    drive(ctx, run_id)
    approval_id = pending_approval(ctx, run_id)
    RunService(ctx).cancel_run(run_id, reason="changed my mind")
    detail = drive(ctx, run_id)
    assert detail.run.status == RunStatus.CANCELLED
    s = steps(ctx, run_id)
    assert s["approve"].status == "cancelled"
    assert s["validate"].status == "skipped"
    with ctx.session() as session:
        assert session.get(ApprovalRequest, approval_id).status == "cancelled"  # type: ignore[union-attr]


def test_cancel_while_children_run_cancels_them(ctx: AppContext) -> None:
    profile = demo_profile(ctx, "timeout", name="slow", timeout_seconds=30)
    definition = {
        "nodes": [
            {
                "key": "slow",
                "type": "agent_run",
                "config": {"agent_profile_id": profile, "prompt": "Take forever."},
            },
            {"key": "after", "type": "transform", "config": {"values": {"x": 1}}},
        ],
        "edges": [{"from": "slow", "to": "after"}],
    }
    run_id = start(ctx, create_workflow(ctx, definition, name="slow wf"))
    # One tick: the workflow starts and creates the child.
    from agenttape.worker.worker import Worker

    worker = Worker(ctx, worker_id="w", concurrency=1)
    asyncio.run(worker.run_until_idle(max_items=1))
    kids = children(ctx, run_id)
    assert len(kids) == 1 and kids[0].status == "queued"
    RunService(ctx).cancel_run(run_id, reason="stop")
    detail = drive(ctx, run_id)
    assert detail.run.status == RunStatus.CANCELLED
    child = RunService(ctx).get_run(kids[0].id)
    assert child.status == RunStatus.CANCELLED
    assert steps(ctx, run_id)["after"].status == "skipped"


@pytest.mark.parametrize("argv", [["git", "push"], ["rm", "-rf", "x"], ["bash", "-c", "ls"]])
def test_disallowed_commands_fail_without_approval(ctx: AppContext, argv: list[str]) -> None:
    definition = {"nodes": [{"key": "bad", "type": "command", "config": {"argv": argv}}]}
    run_id = start(ctx, create_workflow(ctx, definition, name=f"bad {argv[0]}"))
    detail = drive(ctx, run_id)
    assert detail.run.status == RunStatus.FAILED
    assert detail.run.failure_category == "policy_violation"
    assert ApprovalService(ctx).list_approvals(run_id=run_id).total == 0
