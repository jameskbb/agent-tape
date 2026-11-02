"""``collect`` node: gather upstream outputs into the workflow result and a JSON artifact."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from agenttape.application.services.artifacts import ArtifactService
from agenttape.domain.enums import EventType
from agenttape.domain.workflows import NodeType, ancestors, render_value
from agenttape.workflows.nodes.base import NodeContext, NodeExecutor, NodeOutcome, register_node


@register_node
class CollectNode(NodeExecutor):
    type = NodeType.COLLECT

    async def start(self, nc: NodeContext) -> NodeOutcome:
        include = nc.config.get("include") or sorted(ancestors(nc.definition, nc.node.key))
        nodes = nc.template_context.get("nodes") or {}
        collected: dict[str, Any] = {}
        for key in include:
            entry = nodes.get(key) or {}
            collected[key] = {"status": entry.get("status"), "outputs": entry.get("outputs")}
        values = render_value(nc.config.get("values") or {}, nc.template_context)
        document = {
            "workflow": nc.run.workflow_name,
            "run_id": nc.run.id,
            "collected": collected,
            "values": values,
        }
        name = str(nc.config.get("artifact_name") or "workflow-output.json")

        def _store() -> str:
            with nc.ctx.session() as session:
                art = ArtifactService(nc.ctx).store(
                    session,
                    run_id=nc.run.id,
                    name=name,
                    data=json.dumps(document, indent=2, sort_keys=True, default=str).encode(),
                    kind="output",
                    media_type="application/json",
                    step_run_id=nc.step.id,
                    metadata={"node_key": nc.node.key},
                )
                return art.id

        artifact_id = await asyncio.to_thread(_store)
        await nc.events.event(
            EventType.ARTIFACT,
            f"Collected outputs of {len(collected)} node(s) into {name}",
            {"artifact_id": artifact_id, "name": name, "kind": "output"},
        )
        return NodeOutcome.ok(
            {"collected": collected, "values": values, "artifact_id": artifact_id}
        )
