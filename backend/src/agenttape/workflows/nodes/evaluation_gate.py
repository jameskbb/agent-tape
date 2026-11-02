"""``evaluation_gate`` node: run evaluators and gate the workflow on the required ones.

The evaluated subject is ``config.target`` (an upstream agent node: its child run's
events, artifacts, file changes, exit code, outputs, usage, and workdir) or, without a
target, the workflow run itself. Results are stored on the workflow run with this step's
id. A gate's evaluators are required unless they say ``"required": false``.

When a required evaluation fails the node fails with ``evaluation_failed`` - unless
``allow_override`` is set, in which case it pauses for an ``evaluation_override``
approval (continuing after a failed required evaluation always needs a person).
"""

from __future__ import annotations

import asyncio
import functools
from pathlib import Path
from typing import Any

from sqlalchemy import select

from agenttape.application.services.evaluation_catalog import (
    EvaluationCatalogService,
    artifact_reader,
)
from agenttape.application.services.evaluations import EvaluationService
from agenttape.domain.enums import EvaluationStatus, FailureCategory
from agenttape.domain.policy import Verdict, check_evaluation_override
from agenttape.domain.workflows import NodeType
from agenttape.evaluations.base import EvaluationContext, EvaluationSpec
from agenttape.infrastructure.db.models import Run, RunAttempt, StepRun
from agenttape.workflows.nodes.base import (
    ApprovalDraft,
    NodeContext,
    NodeExecutor,
    NodeOutcome,
    StepData,
    register_node,
)
from agenttape.workflows.nodes.command import default_workdir


def _subject(nc: NodeContext) -> dict[str, Any]:
    """Load what the evaluators look at: a target node's child run, or the workflow run."""
    target = nc.config.get("target")
    with nc.ctx.session() as session:
        run_id = nc.run.id
        if target:
            step = session.scalars(
                select(StepRun).where(StepRun.run_id == nc.run.id, StepRun.node_key == target)
            ).first()
            if step is not None and step.child_run_id:
                run_id = step.child_run_id
        run = session.get(Run, run_id)
        assert run is not None
        attempt = session.scalars(
            select(RunAttempt)
            .where(RunAttempt.run_id == run_id)
            .order_by(RunAttempt.attempt_number.desc())
            .limit(1)
        ).first()
        now = nc.ctx.clock.now()
        duration = run.duration_ms
        if duration is None and run.started_at is not None:
            duration = int((now - run.started_at).total_seconds() * 1000)
        return {
            "run_id": run.id,
            "attempt_id": attempt.id if attempt else None,
            "exit_code": attempt.exit_code if attempt else None,
            "outcome": run.status,
            "outputs": dict(run.outputs or {}),
            "workdir": attempt.workdir if attempt and attempt.workdir else None,
            "duration_ms": duration,
            "usage": run.usage,
            "cost": run.cost,
            "is_child": run.id != nc.run.id,
        }


@register_node
class EvaluationGateNode(NodeExecutor):
    type = NodeType.EVALUATION_GATE

    async def start(self, nc: NodeContext) -> NodeOutcome:
        c = nc.config
        specs: list[EvaluationSpec] = []
        for raw in c.get("evaluators") or []:
            data = dict(raw)
            data.setdefault("required", True)
            specs.append(EvaluationSpec.from_dict(data))
        catalog = EvaluationCatalogService(nc.ctx)
        specs.extend(
            await asyncio.to_thread(
                catalog.resolve_specs, list(c.get("definition_ids") or []), c.get("suite_id")
            )
        )
        subject = await asyncio.to_thread(_subject, nc)
        service = EvaluationService(nc.ctx)
        loaders = service.loaders(subject["run_id"])
        workdir = Path(subject["workdir"]) if subject["workdir"] else default_workdir(nc)
        ectx = EvaluationContext(
            run_id=subject["run_id"],
            attempt_id=subject["attempt_id"],
            step_run_id=nc.step.id,
            exit_code=subject["exit_code"],
            outcome=subject["outcome"],
            outputs=subject["outputs"],
            workdir=workdir,
            duration_ms=subject["duration_ms"],
            usage=subject["usage"],
            extras={
                "project_settings": nc.run.project_settings,
                "workflow_run_id": nc.run.id,
                "cancel": nc.cancel,
                "cost": subject["cost"],
                "read_artifact": artifact_reader(nc.ctx),
            },
            **loaders,
        )
        results: list[dict[str, Any]] = []
        failed_required: list[str] = []
        for spec in specs:
            outcome, took, version = await service.evaluate(spec, ectx)
            outcome.evidence.setdefault("subject_run_id", subject["run_id"])
            saved = await asyncio.to_thread(
                functools.partial(
                    service.persist,
                    run_id=nc.run.id,
                    attempt_id=None,
                    step_run_id=nc.step.id,
                    spec=spec,
                    outcome=outcome,
                    duration_ms=took,
                    evaluator_version=version,
                    node_key=nc.node.key,
                )
            )
            name = spec.name or spec.type
            results.append(
                {
                    "id": saved.id,
                    "name": name,
                    "type": spec.type,
                    "status": outcome.status.value,
                    "required": spec.required,
                    "score": outcome.score,
                }
            )
            if spec.required and outcome.status in (EvaluationStatus.FAIL, EvaluationStatus.ERROR):
                failed_required.append(name)
        outputs: dict[str, Any] = {
            "passed": not failed_required,
            "failed": failed_required,
            "total": len(results),
            "results": results,
            "statuses": {r["name"]: r["status"] for r in results},
            "subject_run_id": subject["run_id"],
        }
        if not failed_required:
            return NodeOutcome.ok(outputs)
        decision = check_evaluation_override(
            override_permitted=bool(c.get("allow_override")), failed=failed_required
        )
        if decision.verdict == Verdict.REQUIRE_APPROVAL:
            assert decision.approval_type is not None
            return NodeOutcome(
                status="waiting",
                outputs=outputs,
                approval=ApprovalDraft(
                    title=f"Continue '{nc.run.workflow_name}' after failed evaluation: "
                    + ", ".join(failed_required),
                    rationale=decision.reason,
                    type=decision.approval_type.value,
                    risk_level=decision.risk_level,
                    requested_action={"failed": failed_required, "node": nc.node.key},
                    expires_in_seconds=c.get("approval_expires_in_seconds"),
                ),
                state={"gate_outputs": outputs},
            )
        return NodeOutcome.fail(
            FailureCategory.EVALUATION_FAILED,
            "Required evaluation failed: " + ", ".join(failed_required),
            retryable=False,
            outputs=outputs,
        )

    def on_approval(self, step: StepData, approved: bool, decision: dict[str, Any]) -> NodeOutcome:
        outputs = dict((step.state or {}).get("gate_outputs") or step.outputs or {})
        if approved:
            outputs["overridden"] = True
            outputs["override"] = decision
            return NodeOutcome.ok(outputs)
        outcome = super().on_approval(step, approved, decision)
        outcome.failure_category = FailureCategory.EVALUATION_FAILED
        outcome.outputs = outputs
        return outcome
