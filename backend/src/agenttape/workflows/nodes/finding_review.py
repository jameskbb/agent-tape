"""``finding_review`` node: challenges an upstream ``agent_run`` node's findings.

Challenges the target's findings through ``FindingReviewService`` - the same request
builder the on-demand "Challenge findings" trigger uses. The child is an ordinary
run created with ``parent_relation="workflow_node"``, so the engine observes it generically
(exactly like ``agent_run``'s own child) and its outputs become this node's outputs; the
workflow engine's wake-up hook only resumes the workflow for that literal relation value.
Review outcomes (upheld/refuted/inconclusive) are always model judgment, never proof - this
node never overrides or supersedes the target's own citation verdicts.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from agenttape.domain.enums import FailureCategory
from agenttape.domain.errors import AgentTapeError
from agenttape.domain.workflows import NodeType
from agenttape.infrastructure.db.models import StepRun
from agenttape.workflows.nodes.base import NodeContext, NodeExecutor, NodeOutcome, register_node


def _target_child_run_id(nc: NodeContext) -> str | None:
    """The ``child_run_id`` of the upstream node named by ``config.target``, or ``None``
    when that node has no step, or no child run yet - never a fallback to the workflow
    run itself (the workflow run has no findings of its own to review)."""
    target = nc.config.get("target")
    with nc.ctx.session() as session:
        step = session.scalars(
            select(StepRun).where(StepRun.run_id == nc.run.id, StepRun.node_key == target)
        ).first()
        return step.child_run_id if step is not None else None


@register_node
class FindingReviewNode(NodeExecutor):
    type = NodeType.FINDING_REVIEW
    inline = False

    async def start(self, nc: NodeContext) -> NodeOutcome:
        target_key = str(nc.config.get("target") or "")
        target_run_id = await asyncio.to_thread(_target_child_run_id, nc)
        if not target_run_id:
            return NodeOutcome.fail(
                FailureCategory.VALIDATION_FAILED,
                f"Node '{target_key}' has no child run whose findings could be challenged",
                retryable=False,
            )
        c = nc.config

        def _start() -> tuple[str, bool]:
            from agenttape.application.services.finding_review import FindingReviewService

            return FindingReviewService(nc.ctx).start_for_workflow_node(
                workflow_run_id=nc.run.id,
                workflow_id=nc.run.workflow_id,
                workflow_version_id=nc.run.workflow_version_id,
                workflow_name=nc.run.workflow_name,
                node_key=nc.node.key,
                node_label=nc.node.label,
                attempt=nc.step.attempt_count,
                target_run_id=target_run_id,
                agent_profile_id=str(c["agent_profile_id"]),
                agent_profile_version_id=c.get("agent_profile_version_id"),
                timeout_seconds=nc.node.timeout_seconds,
                max_attempts=nc.node.max_attempts,
                is_demo=nc.run.is_demo,
                actor=f"workflow:{nc.run.id}",
            )

        try:
            child_id, created = await asyncio.to_thread(_start)
        except AgentTapeError as exc:
            return NodeOutcome.fail(
                FailureCategory.VALIDATION_FAILED,
                f"Could not start the finding review: {exc.message}",
                retryable=False,
                outputs={"details": exc.details},
            )
        await nc.events.event(
            "step",
            f"Node '{nc.node.label}' started review run {child_id[:8]}"
            + ("" if created else " (existing child reused)"),
            {"child_run_id": child_id, "created": created},
        )
        return NodeOutcome(status="running", child_run_id=child_id)
