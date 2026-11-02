"""``transform`` node: render a prompt/text template or reshape earlier outputs."""

from __future__ import annotations

from typing import Any

from agenttape.domain.workflows import NodeType, render_value
from agenttape.workflows.nodes.base import NodeContext, NodeExecutor, NodeOutcome, register_node


@register_node
class TransformNode(NodeExecutor):
    type = NodeType.TRANSFORM

    async def start(self, nc: NodeContext) -> NodeOutcome:
        outputs: dict[str, Any] = {}
        if "values" in nc.config:
            values = render_value(nc.config["values"], nc.template_context)
            outputs.update(values if isinstance(values, dict) else {"value": values})
        if "template" in nc.config:
            rendered = render_value(str(nc.config["template"]), nc.template_context)
            outputs["text"] = rendered if isinstance(rendered, str) else str(rendered)
        return NodeOutcome.ok(outputs)
