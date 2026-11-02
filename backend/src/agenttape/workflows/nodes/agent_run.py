"""``agent_run`` node: run an agent profile as a child run through ``RunService``.

The child is an ordinary run (``parent_relation="workflow_node"``) with an idempotency key
``wf:<workflow run>:<node>:<attempt>``, so a restarted engine that retries the start finds
the existing child instead of creating a duplicate. The engine does not hold a worker
slot while the child runs: the child's terminal hook wakes the workflow run.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agenttape.application.schemas.runs import EvaluatorConfig, RunCreate
from agenttape.domain.enums import FailureCategory, RunSource, VariableType
from agenttape.domain.errors import AgentTapeError
from agenttape.domain.workflows import (
    NodeType,
    TemplateError,
    render_value,
    template_references,
)
from agenttape.workflows.nodes.base import NodeContext, NodeExecutor, NodeOutcome, register_node


def child_idempotency_key(run_id: str, node_key: str, attempt: int) -> str:
    return f"wf:{run_id}:{node_key}:{attempt}"


@register_node
class AgentRunNode(NodeExecutor):
    type = NodeType.AGENT_RUN
    inline = False

    async def start(self, nc: NodeContext) -> NodeOutcome:
        c = nc.config
        prompt: str | None = None
        if c.get("prompt_template_id"):
            tainted = await asyncio.to_thread(_secrets_from_node_outputs, nc)
            if tainted:
                return NodeOutcome.fail(
                    FailureCategory.POLICY_VIOLATION,
                    f"The secret variable '{tainted}' is filled from another node's outputs; "
                    "an agent or command must never choose which secret is resolved. Give the "
                    "node a fixed secret reference name.",
                    retryable=False,
                )
        if not c.get("prompt_template_id"):
            try:
                rendered = render_value(str(c.get("prompt") or ""), nc.template_context)
            except TemplateError as exc:
                return NodeOutcome.fail(FailureCategory.CONFIG_ERROR, str(exc), retryable=False)
            prompt = rendered if isinstance(rendered, str) else str(rendered)
        evaluators: list[EvaluatorConfig] | None = None
        if isinstance(c.get("evaluators"), list):
            evaluators = [EvaluatorConfig.model_validate(e) for e in c["evaluators"]]
        request = RunCreate(
            kind="agent",
            title=f"{nc.run.workflow_name}: {nc.node.label}"[:300],
            project_id=nc.run.project_id,
            agent_profile_id=str(c["agent_profile_id"]),
            agent_profile_version_id=c.get("agent_profile_version_id"),
            prompt_template_id=c.get("prompt_template_id"),
            prompt_version_id=c.get("prompt_version_id"),
            prompt=prompt,
            inputs=dict(nc.step.inputs or {}) if c.get("prompt_template_id") else {},
            source=RunSource.WORKFLOW,
            source_ref=f"{nc.run.id}:{nc.node.key}",
            idempotency_key=child_idempotency_key(nc.run.id, nc.node.key, nc.step.attempt_count),
            parent_run_id=nc.run.id,
            parent_relation="workflow_node",
            parent_node_key=nc.node.key,
            workflow_id=nc.run.workflow_id,
            workflow_version_id=nc.run.workflow_version_id,
            evaluators=evaluators,
            max_attempts=nc.node.max_attempts if nc.node.max_attempts > 1 else None,
            timeout_seconds=nc.node.timeout_seconds,
            is_demo=nc.run.is_demo or None,
            tags=["workflow"],
        )
        from agenttape.application.services.runs import RunService

        def _create() -> tuple[str, bool]:
            result = RunService(nc.ctx).create_run(request, actor=f"workflow:{nc.run.id}")
            return result.run.id, result.created

        try:
            child_id, created = await asyncio.to_thread(_create)
        except AgentTapeError as exc:
            return NodeOutcome.fail(
                FailureCategory.CONFIG_ERROR,
                f"Could not start the agent run: {exc.message}",
                retryable=False,
                outputs={"details": exc.details},
            )
        await nc.events.event(
            "step",
            f"Node '{nc.node.label}' started child run {child_id[:8]}"
            + ("" if created else " (existing child reused)"),
            {"child_run_id": child_id, "created": created},
        )
        return NodeOutcome(status="running", child_run_id=child_id)


def outputs_from_child(child: Any, workdir: str | None) -> dict[str, Any]:
    """The node's outputs: the child's outputs plus its identity and status."""
    out: dict[str, Any] = dict(child.outputs or {})
    out.update(
        {
            "child_run_id": child.id,
            "status": child.status,
            "workdir": workdir,
            "failure_category": child.failure_category,
            "duration_ms": child.duration_ms,
        }
    )
    return out


def _secrets_from_node_outputs(nc: NodeContext) -> str | None:
    """The first secret-typed prompt variable this node fills from ``{{ nodes.* }}``.

    A secret variable's value is a reference *name*; a name taken from agent or command
    output would let that output pick any secret the worker can resolve.
    """
    from sqlalchemy import select

    from agenttape.infrastructure.db.models import PromptVersion

    c = nc.config
    with nc.ctx.session() as session:
        query = select(PromptVersion.variables)
        if c.get("prompt_version_id"):
            query = query.where(PromptVersion.id == str(c["prompt_version_id"]))
        else:
            query = query.where(PromptVersion.template_id == str(c["prompt_template_id"])).order_by(
                PromptVersion.version.desc()
            )
        variables = session.scalars(query.limit(1)).first() or []
    for var in variables:
        if not isinstance(var, dict) or var.get("type") != VariableType.SECRET.value:
            continue
        name = str(var.get("name"))
        if any(ref.startswith("nodes.") for ref in template_references(nc.node.inputs.get(name))):
            return name
    return None
