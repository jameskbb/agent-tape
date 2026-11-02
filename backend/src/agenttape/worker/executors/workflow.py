"""Executor for ``kind == "workflow"`` runs: one engine tick per claimed queue item.

The workflow run never holds a worker slot while its child runs execute: each tick
settles what it can, completes its queue item, and returns. Wake-ups:

* a child run of a workflow node finishes or pauses -> ``on_run_terminal`` hook below;
* an approval requested by a node is decided -> the ``workflow_node`` approval handler
  (``application/services/workflows.py``);
* the workflow run (or one of its children) is cancelled -> the cancel hook registered
  in ``application/services/workflows.py``;
* anything else that was lost (crash, restart) -> the maintenance sweep
  (``agenttape/workflows/maintenance.py``).
"""

from __future__ import annotations

import asyncio

from agenttape.application.context import AppContext
from agenttape.application.ports import ClaimedItem
from agenttape.application.services.workflows import WORKFLOW_KIND, wake_workflow
from agenttape.infrastructure.db.models import Run
from agenttape.worker.hooks import register_on_run_terminal
from agenttape.worker.registry import RunExecutor, WorkerRuntime, register_executor
from agenttape.workflows.engine import WorkflowEngine


@register_executor
class WorkflowRunExecutor(RunExecutor):
    kind = WORKFLOW_KIND

    async def execute(self, runtime: WorkerRuntime, claim: ClaimedItem) -> None:
        try:
            if claim.run_id:
                await WorkflowEngine(runtime.ctx, runtime.worker_id).tick(claim.run_id, claim.id)
        finally:
            await asyncio.to_thread(runtime.ctx.queue.complete, claim.id, runtime.worker_id)


def wake_parent(ctx: AppContext, run_id: str) -> None:
    with ctx.session() as session:
        child = session.get(Run, run_id)
        if child is None or child.parent_relation != "workflow_node" or not child.parent_run_id:
            return
        parent = session.get(Run, child.parent_run_id)
        if parent is None or parent.kind != WORKFLOW_KIND:
            return
        wake_workflow(ctx, session, parent, f"Workflow resuming: '{child.parent_node_key}' changed")


async def _on_child_terminal(ctx: AppContext, run_id: str, status: str) -> None:
    await asyncio.to_thread(wake_parent, ctx, run_id)


register_on_run_terminal(_on_child_terminal)
