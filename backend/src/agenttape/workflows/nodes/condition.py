"""``condition`` node: evaluate an expression; outgoing edges pick the true/false branch."""

from __future__ import annotations

from agenttape.domain.enums import EventType, FailureCategory
from agenttape.domain.workflows import ConditionError, NodeType, parse_condition
from agenttape.workflows.nodes.base import NodeContext, NodeExecutor, NodeOutcome, register_node


@register_node
class ConditionNode(NodeExecutor):
    type = NodeType.CONDITION

    async def start(self, nc: NodeContext) -> NodeOutcome:
        expression = str(nc.config.get("expression") or "")
        try:
            result = parse_condition(expression).evaluate(nc.template_context)
        except ConditionError as exc:
            return NodeOutcome.fail(FailureCategory.CONFIG_ERROR, str(exc), retryable=False)
        await nc.events.event(
            EventType.STEP,
            f"Condition '{nc.node.label}' is {'true' if result else 'false'}",
            {"expression": expression, "result": result},
        )
        return NodeOutcome.ok({"result": result, "expression": expression})
