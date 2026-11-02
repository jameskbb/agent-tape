"""``approval`` node: pause durably until a person decides in the approval inbox."""

from __future__ import annotations

from typing import Any

from agenttape.domain.enums import RiskLevel
from agenttape.domain.policy import ApprovalType
from agenttape.domain.workflows import NodeType, render_value
from agenttape.workflows.nodes.base import (
    ApprovalDraft,
    NodeContext,
    NodeExecutor,
    NodeOutcome,
    StepData,
    register_node,
)


@register_node
class ApprovalNode(NodeExecutor):
    type = NodeType.APPROVAL
    inline = False

    async def start(self, nc: NodeContext) -> NodeOutcome:
        c = nc.config
        title = render_value(str(c.get("title") or nc.node.label), nc.template_context)
        rationale = c.get("rationale")
        rationale_text = (
            str(render_value(str(rationale), nc.template_context)) if rationale else None
        )
        requested: Any = render_value(c.get("requested_action") or {}, nc.template_context)
        if not isinstance(requested, dict):
            requested = {"value": requested}
        requested.setdefault("node", nc.node.key)
        requested.setdefault("workflow", nc.run.workflow_name)
        if nc.step.inputs:
            requested.setdefault("inputs", nc.step.inputs)
        expires = c.get("expires_in_seconds") or nc.node.timeout_seconds
        try:
            risk = RiskLevel(str(c.get("risk_level") or "medium"))
        except ValueError:
            risk = RiskLevel.MEDIUM
        return NodeOutcome(
            status="waiting",
            approval=ApprovalDraft(
                title=str(title),
                rationale=rationale_text,
                type=str(c.get("approval_type") or ApprovalType.WORKFLOW_NODE.value),
                risk_level=risk,
                requested_action=requested,
                expires_in_seconds=float(expires) if expires else None,
            ),
        )

    def on_approval(self, step: StepData, approved: bool, decision: dict[str, Any]) -> NodeOutcome:
        if approved:
            return NodeOutcome.ok(
                {
                    "approved": True,
                    "reviewer": decision.get("reviewer"),
                    "note": decision.get("note"),
                    "approval_id": decision.get("approval_id"),
                }
            )
        return super().on_approval(step, approved, decision)
