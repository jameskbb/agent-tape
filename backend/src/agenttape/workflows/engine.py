"""The durable workflow engine.

One *tick* evaluates one workflow run: it reads the persisted ``step_runs`` rows, observes
child runs and approvals, starts every node whose dependencies are settled (up to the
workflow's ``max_parallel``), runs inline nodes to completion, and settles the run's
status. Nothing lives only in memory between ticks, so a tick can stop at any point (the
process may crash) and the next tick continues from the database:

* a settled step (``succeeded``/``failed``/``skipped``...) is never started again;
* an agent node's child run is created with an idempotency key derived from the run,
  node, and step attempt, so re-issuing the start after a crash finds the same child;
* an approval step waits on its ``approval_requests`` row; the decision is read on the
  next tick, whoever made it;
* an inline node found ``running`` at the start of a tick was interrupted by a crash (a
  live tick holds the per-run lease), so it is re-queued as a new attempt.

Ticks for one run are serialized by a named lease (``wf:<run id>``); a tick that cannot
get the lease re-enqueues the run shortly after. Ticks are triggered by the queue: run
creation, child runs finishing (terminal hook), approval decisions (the
``workflow_node`` approval handler), cancellation (cancel hook), retry backoff, and the
maintenance sweep (``workflows/maintenance.py``) as a safety net.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import jsonschema
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from agenttape.adapters.base import CancellationToken, EventEmitter
from agenttape.application.context import AppContext
from agenttape.application.events import EventRecorder
from agenttape.application.services.approvals import ApprovalService
from agenttape.application.services.audit import record_audit
from agenttape.application.services.runs import RunService
from agenttape.application.services.workflows import APPROVAL_HANDLER
from agenttape.application.transitions import transition_run, transition_step
from agenttape.domain.enums import (
    ApprovalStatus,
    EventType,
    FailureCategory,
    RunStatus,
    Severity,
)
from agenttape.domain.errors import AgentTapeError
from agenttape.domain.events import EventDraft
from agenttape.domain.prompts import validate_inputs
from agenttape.domain.workflows import (
    TERMINAL_STEP_STATES,
    NodeDef,
    NodeType,
    TemplateError,
    WorkflowDefinition,
    failure_is_fatal,
    plan_ready,
    render_value,
    topological_order,
)
from agenttape.infrastructure.db.models import (
    ApprovalRequest,
    Run,
    RunAttempt,
    StepRun,
    WorkflowVersion,
)
from agenttape.security.redaction import Redactor
from agenttape.worker import hooks
from agenttape.worker.sink import BatchingEventSink
from agenttape.workflows.nodes.agent_run import outputs_from_child
from agenttape.workflows.nodes.base import (
    NodeContext,
    NodeExecutor,
    NodeOutcome,
    RunData,
    StepData,
    load_node_executors,
)

log = logging.getLogger("agenttape.workflows")

MAX_ITERATIONS = 200
WATCH_SECONDS = 0.5
BUSY_RETRY_SECONDS = 0.5
ACTIVE = RunStatus.RUNNING.value
WAITING = RunStatus.WAITING_FOR_APPROVAL.value
QUEUED = RunStatus.QUEUED.value
CHILD_ACTIVE = frozenset({"queued", "preparing", "running", "cancelling"})
CHILD_FAILED = {"failed": RunStatus.FAILED, "timed_out": RunStatus.TIMED_OUT}


def lease_name(run_id: str) -> str:
    return f"wf:{run_id}"


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _category(value: str | None) -> FailureCategory:
    try:
        return FailureCategory(value or "")
    except ValueError:
        return FailureCategory.DEPENDENCY_FAILED


def _step_data(s: StepRun) -> StepData:
    return StepData(
        id=s.id,
        key=s.node_key,
        status=s.status,
        attempt_count=s.attempt_count,
        state=dict(s.state or {}),
        child_run_id=s.child_run_id,
        approval_request_id=s.approval_request_id,
        inputs=dict(s.inputs) if s.inputs is not None else None,
        outputs=dict(s.outputs) if s.outputs is not None else None,
        failure_category=s.failure_category,
        failure_message=s.failure_message,
    )


@dataclass
class Loaded:
    run: RunData
    definition: WorkflowDefinition
    steps: dict[str, StepData] = field(default_factory=dict)


@dataclass
class SyncResult:
    run_status: str
    steps: dict[str, StepData]
    changed: bool
    #: agent steps marked running whose child was never recorded (crash mid-start)
    resume_starts: list[str] = field(default_factory=list)


class WorkflowEngine:
    def __init__(self, ctx: AppContext, worker_id: str) -> None:
        self.ctx = ctx
        self.worker_id = worker_id
        self.nodes: dict[NodeType, NodeExecutor] = load_node_executors()
        self.recorder = EventRecorder(ctx.settings, ctx.clock)

    # ================================================================== entry point

    async def tick(self, run_id: str, claim_id: str | None = None) -> None:
        ttl = max(5.0, self.ctx.settings.lease_seconds)
        got = await asyncio.to_thread(
            self.ctx.leases.acquire, lease_name(run_id), self.worker_id, ttl
        )
        if not got:
            await asyncio.to_thread(self._requeue, run_id, BUSY_RETRY_SECONDS)
            return
        try:
            await self._tick(run_id, claim_id)
        finally:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.ctx.leases.release, lease_name(run_id), self.worker_id)

    def _requeue(self, run_id: str, delay: float) -> None:
        with self.ctx.session() as session:
            run = session.get(Run, run_id)
            if run is None or run.status in ("succeeded", "failed", "timed_out", "skipped"):
                return
            RunService(self.ctx).enqueue(
                session, run, available_at=self.ctx.clock.now() + timedelta(seconds=delay)
            )

    async def _tick(self, run_id: str, claim_id: str | None) -> None:
        loaded = await asyncio.to_thread(self._begin, run_id)
        if loaded is None:
            return
        if loaded.run.status in (RunStatus.CANCELLING.value, RunStatus.CANCELLED.value):
            await self._cancel(run_id)
            return
        cancel = CancellationToken()
        sink = BatchingEventSink(
            run_id=run_id,
            recorder=self.recorder,
            session_factory=self.ctx.session_factory,
            redactor=Redactor(),
            started_monotonic=time.monotonic(),
        )
        await sink.start()
        watch = asyncio.create_task(self._watch(run_id, claim_id, cancel))
        node_tokens: list[CancellationToken] = []
        cancel_forward = asyncio.create_task(self._forward_cancel(cancel, node_tokens))
        failed_fast = False
        try:
            for _ in range(MAX_ITERATIONS):
                sync = await asyncio.to_thread(self._sync, loaded)
                loaded.steps = sync.steps
                if cancel.cancelled or sync.run_status in (
                    RunStatus.CANCELLING.value,
                    RunStatus.CANCELLED.value,
                ):
                    break
                fatal = self._fatal_step(loaded)
                if fatal is not None:
                    await asyncio.to_thread(self._fail_fast, loaded, fatal)
                    failed_fast = True
                    break
                for key in sync.resume_starts:
                    await self._start_node(loaded, key, sink, cancel, node_tokens, resume=True)
                statuses = {k: s.status for k, s in loaded.steps.items()}
                outputs = {k: s.outputs for k, s in loaded.steps.items()}
                ready = plan_ready(loaded.definition, statuses, outputs, self._context(loaded))
                if ready.skip:
                    await asyncio.to_thread(self._skip, loaded, ready.skip)
                    continue
                starts = self._startable(loaded, ready.run)
                if not starts:
                    if sync.changed or sync.resume_starts:
                        continue
                    break
                await self._start_batch(loaded, starts, sink, cancel, node_tokens)
        finally:
            watch.cancel()
            cancel_forward.cancel()
            for task in (watch, cancel_forward):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            await sink.close()
        if failed_fast:
            await self._after_terminal(run_id, RunStatus.FAILED.value)
            return
        status_now = await asyncio.to_thread(self._run_status, run_id)
        if cancel.cancelled or status_now in (
            RunStatus.CANCELLING.value,
            RunStatus.CANCELLED.value,
        ):
            await self._cancel(run_id)
            return
        final, wake_at, transitioned = await asyncio.to_thread(self._finalize, loaded)
        if wake_at is not None:
            await asyncio.to_thread(
                self._requeue,
                run_id,
                max(0.0, (wake_at - self.ctx.clock.now()).total_seconds()),
            )
        if transitioned and final in ("succeeded", "waiting_for_approval"):
            await self._after_terminal(run_id, final)

    async def _after_terminal(self, run_id: str, status: str) -> None:
        await hooks.fire_run_terminal(self.ctx, run_id, status)

    def _run_status(self, run_id: str) -> str:
        with self.ctx.session() as session:
            run = session.get(Run, run_id)
            return run.status if run is not None else "deleted"

    # ================================================================== begin

    def _begin(self, run_id: str) -> Loaded | None:
        with self.ctx.session() as session:
            run = session.get(Run, run_id)
            if run is None or run.kind != "workflow":
                return None
            if run.status in ("succeeded", "failed", "timed_out", "skipped"):
                return None
            section = dict((run.snapshot or {}).get("workflow") or {})
            try:
                definition = WorkflowDefinition.model_validate(section.get("definition") or {})
            except Exception as exc:
                self._fail_run_now(
                    session, run, FailureCategory.CONFIG_ERROR, f"Invalid workflow snapshot: {exc}"
                )
                return None
            if not definition.nodes:
                self._fail_run_now(
                    session,
                    run,
                    FailureCategory.CONFIG_ERROR,
                    "The run has no workflow definition in its snapshot",
                )
                return None
            # Runs created by schedules and webhooks carry raw inputs: apply the declared
            # defaults and types here, the same way WorkflowService.start_run does.
            inputs, issues = validate_inputs(definition.inputs, dict(run.inputs or {}))
            if issues:
                self._fail_run_now(
                    session,
                    run,
                    FailureCategory.CONFIG_ERROR,
                    "Workflow inputs are invalid: "
                    + "; ".join(f"{i.variable} {i.message}" for i in issues),
                )
                return None
            if run.workflow_version_id:
                version = session.get(WorkflowVersion, run.workflow_version_id)
                if version is not None and version.locked_at is None:
                    version.locked_at = self.ctx.clock.now()
            order = topological_order(
                [n.key for n in definition.nodes], [(e.source, e.target) for e in definition.edges]
            ) or [n.key for n in definition.nodes]
            steps = {
                s.node_key: s
                for s in session.scalars(select(StepRun).where(StepRun.run_id == run.id))
            }
            now = self.ctx.clock.now()
            if run.status == QUEUED:
                # A queued run whose steps are all settled can only be a manual retry of a
                # failed or cancelled workflow (succeeded runs cannot be retried).
                retried = bool(steps) and all(
                    s.status in TERMINAL_STEP_STATES for s in steps.values()
                )
                first = not steps
                transition_run(
                    session,
                    run,
                    RunStatus.PREPARING,
                    started_at=run.started_at or now,
                    attempt_count=(run.attempt_count or 0) + (1 if first or retried else 0),
                )
                for position, key in enumerate(order):
                    if key in steps:
                        continue
                    node = definition.node(key)
                    step = StepRun(
                        run_id=run.id,
                        node_key=key,
                        node_type=node.type.value,
                        name=node.label,
                        status=QUEUED,
                        position=position,
                        attempt_count=0,
                        state={},
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(step)
                    steps[key] = step
                session.flush()
                if retried:
                    reset = [s for s in steps.values() if s.status != "succeeded"]
                    for s in reset:
                        if s.status in ("failed", "timed_out", "cancelled"):
                            transition_step(
                                session,
                                s,
                                RunStatus.QUEUED,
                                child_run_id=None,
                                approval_request_id=None,
                                failure_category=None,
                                failure_message=None,
                                finished_at=None,
                                duration_ms=None,
                                state={**dict(s.state or {}), "retry_at": None},
                            )
                        elif s.status == "skipped":
                            # ``skipped`` is final in the step state machine; a manual retry of
                            # the whole workflow is the one sanctioned way back to ``queued``.
                            session.execute(
                                update(StepRun)
                                .where(StepRun.id == s.id)
                                .values(
                                    status=QUEUED, finished_at=None, state={}, failure_message=None
                                )
                            )
                    self._event(
                        session,
                        run.id,
                        f"Retrying workflow: {len(reset)} unfinished node(s) re-queued; "
                        "succeeded nodes are kept",
                        {"requeued": sorted(s.node_key for s in reset)},
                    )
                transition_run(session, run, RunStatus.RUNNING)
                self.recorder.record(
                    session,
                    run.id,
                    EventType.LIFECYCLE,
                    "Workflow started" if first else "Workflow resumed",
                    {
                        "status": "running",
                        "nodes": len(order),
                        "max_parallel": definition.settings.max_parallel,
                    },
                )
            elif run.status == RunStatus.PREPARING.value:
                transition_run(session, run, RunStatus.RUNNING)
            session.flush()
            data = RunData(
                id=run.id,
                title=run.title,
                workspace_id=run.workspace_id,
                project_id=run.project_id,
                is_demo=run.is_demo,
                inputs=inputs,
                snapshot=dict(run.snapshot or {}),
                workflow_id=run.workflow_id,
                workflow_version_id=run.workflow_version_id,
                status=run.status,
            )
            return Loaded(
                run=data,
                definition=definition,
                steps={k: _step_data(s) for k, s in steps.items()},
            )

    # ================================================================== sync

    def _sync(self, loaded: Loaded) -> SyncResult:
        """Observe the outside world: child runs, approvals, interrupted inline steps."""
        changed = False
        resume: list[str] = []
        with self.ctx.session() as session:
            run = session.get(Run, loaded.run.id)
            assert run is not None
            steps = {
                s.node_key: s
                for s in session.scalars(select(StepRun).where(StepRun.run_id == run.id))
            }
            now = self.ctx.clock.now()
            for key, step in steps.items():
                if key not in loaded.definition.node_map():
                    continue
                node = loaded.definition.node(key)
                executor = self.nodes[node.type]
                if step.status == WAITING and step.approval_request_id:
                    approval = session.get(ApprovalRequest, step.approval_request_id)
                    if approval is None or approval.status == ApprovalStatus.PENDING.value:
                        continue
                    decision = {
                        "approval_id": approval.id,
                        "status": approval.status,
                        "reviewer": approval.reviewer,
                        "note": approval.reviewer_note,
                        "resolved_at": approval.resolved_at.isoformat()
                        if approval.resolved_at
                        else None,
                    }
                    approved = approval.status == ApprovalStatus.APPROVED.value
                    if approval.status == ApprovalStatus.CANCELLED.value:
                        self._settle_step(
                            session,
                            run,
                            node,
                            step,
                            NodeOutcome(status="cancelled", message="Approval was cancelled"),
                        )
                    else:
                        outcome = executor.on_approval(_step_data(step), approved, decision)
                        if outcome.status == "running":
                            transition_step(
                                session,
                                step,
                                RunStatus.QUEUED,
                                approval_request_id=None,
                                state={**dict(step.state or {}), **outcome.state},
                            )
                            self._event(
                                session,
                                run.id,
                                f"Approval granted for '{node.label}'; it will run now",
                                {"approval_id": approval.id},
                                node_key=key,
                                step_run_id=step.id,
                            )
                        else:
                            self._settle_step(session, run, node, step, outcome)
                    changed = True
                elif step.child_run_id and step.status in (ACTIVE, WAITING):
                    child = session.get(Run, step.child_run_id)
                    if child is None:
                        self._settle_step(
                            session,
                            run,
                            node,
                            step,
                            NodeOutcome.fail(
                                FailureCategory.INTERNAL_ERROR,
                                "Child run was deleted",
                                retryable=False,
                            ),
                        )
                        changed = True
                        continue
                    if (
                        child.status == "succeeded"
                        or child.status in CHILD_FAILED
                        or child.status in ("cancelled", "skipped")
                    ):
                        workdir = session.scalars(
                            select(RunAttempt.workdir)
                            .where(RunAttempt.run_id == child.id)
                            .order_by(RunAttempt.attempt_number.desc())
                            .limit(1)
                        ).first()
                        outputs = outputs_from_child(child, workdir)
                        if child.status == "succeeded":
                            outcome = NodeOutcome.ok(outputs)
                        else:
                            category = _category(child.failure_category)
                            outcome = NodeOutcome(
                                status="timed_out" if child.status == "timed_out" else "failed",
                                failure_category=category,
                                message=f"Child run {child.status}: {child.failure_message or category.value}",
                                outputs=outputs,
                                retryable=False,
                            )
                        if step.status == WAITING:
                            transition_step(session, step, RunStatus.RUNNING)
                        self._settle_step(session, run, node, step, outcome)
                        changed = True
                    elif child.status == WAITING and step.status == ACTIVE:
                        transition_step(session, step, RunStatus.WAITING_FOR_APPROVAL)
                        self._event(
                            session,
                            run.id,
                            f"'{node.label}' is waiting for approval in its child run",
                            {"child_run_id": child.id},
                            node_key=key,
                            step_run_id=step.id,
                            severity=Severity.NOTICE,
                        )
                        changed = True
                    elif child.status in CHILD_ACTIVE and step.status == WAITING:
                        transition_step(session, step, RunStatus.RUNNING)
                        changed = True
                elif step.status == ACTIVE and not step.child_run_id:
                    if not executor.inline:
                        resume.append(key)  # agent start interrupted before the child id was saved
                        continue
                    # An inline node can only be ``running`` here if a previous tick died.
                    transition_step(
                        session,
                        step,
                        RunStatus.FAILED,
                        failure_category=FailureCategory.INTERRUPTED.value,
                        failure_message="The engine stopped while this node was running",
                        finished_at=now,
                    )
                    transition_step(
                        session,
                        step,
                        RunStatus.QUEUED,
                        failure_category=None,
                        failure_message=None,
                        finished_at=None,
                    )
                    self._event(
                        session,
                        run.id,
                        f"'{node.label}' was interrupted (attempt {step.attempt_count}); "
                        "it will run again because it never completed",
                        {"attempt": step.attempt_count},
                        node_key=key,
                        step_run_id=step.id,
                        severity=Severity.WARNING,
                    )
                    changed = True
            session.flush()
            for s in steps.values():
                session.refresh(s)
            return SyncResult(
                run_status=run.status,
                steps={k: _step_data(s) for k, s in steps.items()},
                changed=changed,
                resume_starts=resume,
            )

    # ================================================================== scheduling

    def _context(self, loaded: Loaded, node_inputs: dict[str, Any] | None = None) -> dict[str, Any]:
        section = loaded.run.snapshot.get("workflow") or {}
        return {
            "inputs": loaded.run.inputs,
            "nodes": {
                k: {"status": s.status, "outputs": s.outputs or {}} for k, s in loaded.steps.items()
            },
            "workflow": {
                "run_id": loaded.run.id,
                "name": section.get("name"),
                "version": section.get("version"),
                "project_id": loaded.run.project_id,
            },
            "node": {"inputs": node_inputs or {}},
        }

    def _fatal_step(self, loaded: Loaded) -> str | None:
        for key, step in loaded.steps.items():
            if (
                step.status in ("failed", "timed_out")
                and (step.state or {}).get("retry_at") is None
            ) and failure_is_fatal(loaded.definition, loaded.definition.node(key)):
                return key
        return None

    def _startable(self, loaded: Loaded, ready: list[str]) -> list[str]:
        active = sum(1 for s in loaded.steps.values() if s.status == ACTIVE)
        slots = max(0, loaded.definition.settings.max_parallel - active)
        now = self.ctx.clock.now()
        out: list[str] = []
        for key in ready:
            retry_at = _parse((loaded.steps[key].state or {}).get("retry_at"))
            if retry_at is not None and retry_at > now:
                continue
            if len(out) >= slots:
                break
            out.append(key)
        return out

    def _skip(self, loaded: Loaded, skips: dict[str, str]) -> None:
        with self.ctx.session() as session:
            now = self.ctx.clock.now()
            for key, reason in skips.items():
                step = session.scalars(
                    select(StepRun).where(StepRun.run_id == loaded.run.id, StepRun.node_key == key)
                ).one()
                if step.status != QUEUED:
                    continue
                transition_step(
                    session,
                    step,
                    RunStatus.SKIPPED,
                    finished_at=now,
                    failure_message=reason[:2000],
                )
                self._event(
                    session,
                    loaded.run.id,
                    f"Skipped '{loaded.definition.node(key).label}': {reason}"[:480],
                    {"status": "skipped", "reason": reason},
                    node_key=key,
                    step_run_id=step.id,
                )

    # ================================================================== starting nodes

    async def _start_batch(
        self,
        loaded: Loaded,
        keys: list[str],
        sink: BatchingEventSink,
        cancel: CancellationToken,
        tokens: list[CancellationToken],
    ) -> None:
        inline: list[str] = []
        for key in keys:
            node = loaded.definition.node(key)
            if self.nodes[node.type].inline:
                inline.append(key)
            else:
                await self._start_node(loaded, key, sink, cancel, tokens)
        if inline:
            await asyncio.gather(
                *(self._start_node(loaded, key, sink, cancel, tokens) for key in inline)
            )

    async def _start_node(
        self,
        loaded: Loaded,
        key: str,
        sink: BatchingEventSink,
        cancel: CancellationToken,
        tokens: list[CancellationToken],
        *,
        resume: bool = False,
    ) -> None:
        node = loaded.definition.node(key)
        executor = self.nodes[node.type]
        inputs: dict[str, Any]
        if resume:
            step = loaded.steps[key]
            inputs = step.inputs or {}
        else:
            prepared = await asyncio.to_thread(self._mark_started, loaded, node)
            if prepared is None:
                return
            step, resolved = prepared
            if isinstance(resolved, NodeOutcome):
                await asyncio.to_thread(self._settle, loaded, node, resolved)
                return
            inputs = resolved
        token = CancellationToken()
        tokens.append(token)
        emitter = EventEmitter(
            sink.emit, source="workflow", attempt_id=None, step_run_id=step.id, node_key=key
        )
        nc = NodeContext(
            ctx=self.ctx,
            run=loaded.run,
            definition=loaded.definition,
            node=node,
            step=step,
            template_context=self._context(loaded, inputs),
            events=emitter,
            cancel=token,
            log_dir=self.ctx.settings.logs_dir / loaded.run.id / key,
            worker_id=self.worker_id,
        )
        nc.log_dir.mkdir(parents=True, exist_ok=True)
        timer: asyncio.Task[None] | None = None
        if node.timeout_seconds and executor.inline:
            timer = asyncio.create_task(self._timeout(token, node.timeout_seconds))
        try:
            if executor.inline and node.timeout_seconds:
                backstop = node.timeout_seconds + self.ctx.settings.cancel_grace_seconds + 10
                outcome = await asyncio.wait_for(executor.start(nc), timeout=backstop)
            else:
                outcome = await executor.start(nc)
        except TimeoutError:
            outcome = NodeOutcome(
                status="timed_out",
                failure_category=FailureCategory.TIMEOUT,
                message=f"Exceeded the node timeout of {node.timeout_seconds:g}s",
            )
        except TemplateError as exc:
            outcome = NodeOutcome.fail(FailureCategory.CONFIG_ERROR, str(exc), retryable=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("workflow node %s crashed", key)
            outcome = NodeOutcome.fail(
                FailureCategory.INTERNAL_ERROR, f"{type(exc).__name__}: {exc}"
            )
        finally:
            if timer is not None:
                timer.cancel()
            with contextlib.suppress(ValueError):
                tokens.remove(token)
        if token.cancelled and outcome.status in ("failed", "timed_out"):
            if token.reason == FailureCategory.TIMEOUT:
                outcome.status = "timed_out"
                outcome.failure_category = FailureCategory.TIMEOUT
                outcome.message = token.message
            elif token.reason == FailureCategory.CANCELLED:
                outcome.status = "cancelled"
        await sink.flush()
        loaded.steps[key] = step
        await asyncio.to_thread(self._settle, loaded, node, outcome)

    @staticmethod
    async def _timeout(token: CancellationToken, seconds: float) -> None:
        if not await token.sleep(seconds):
            token.cancel(FailureCategory.TIMEOUT, f"Exceeded the node timeout of {seconds:g}s")

    def _mark_started(
        self, loaded: Loaded, node: NodeDef
    ) -> tuple[StepData, dict[str, Any] | NodeOutcome] | None:
        """queued -> running (attempt + 1) with resolved, schema-checked inputs."""
        with self.ctx.session() as session:
            step = session.scalars(
                select(StepRun).where(StepRun.run_id == loaded.run.id, StepRun.node_key == node.key)
            ).one()
            if step.status != QUEUED:
                return None
            problem: NodeOutcome | None = None
            inputs: dict[str, Any] = {}
            try:
                resolved = render_value(node.inputs, self._context(loaded))
                inputs = resolved if isinstance(resolved, dict) else {"value": resolved}
            except TemplateError as exc:
                problem = NodeOutcome.fail(
                    FailureCategory.CONFIG_ERROR,
                    f"Inputs could not be resolved: {exc}",
                    retryable=False,
                )
            if problem is None and node.input_schema:
                try:
                    jsonschema.validate(inputs, node.input_schema)
                except jsonschema.ValidationError as exc:
                    problem = NodeOutcome.fail(
                        FailureCategory.VALIDATION_FAILED,
                        f"Inputs do not match the input schema: {exc.message}",
                        retryable=False,
                    )
                except jsonschema.SchemaError as exc:
                    problem = NodeOutcome.fail(
                        FailureCategory.CONFIG_ERROR,
                        f"Invalid input schema: {exc.message}",
                        retryable=False,
                    )
            now = self.ctx.clock.now()
            state = {k: v for k, v in dict(step.state or {}).items() if k != "retry_at"}
            transition_step(
                session,
                step,
                RunStatus.RUNNING,
                attempt_count=step.attempt_count + 1,
                started_at=now,
                finished_at=None,
                inputs=inputs,
                state=state,
                failure_category=None,
                failure_message=None,
            )
            session.refresh(step)
            self._event(
                session,
                loaded.run.id,
                f"Node '{node.label}' started"
                + (
                    f" (attempt {step.attempt_count} of {node.max_attempts})"
                    if step.attempt_count > 1
                    else ""
                ),
                {"status": "running", "node_type": node.type.value, "attempt": step.attempt_count},
                node_key=node.key,
                step_run_id=step.id,
            )
            return _step_data(step), (problem if problem is not None else inputs)

    # ================================================================== settling

    def _settle(self, loaded: Loaded, node: NodeDef, outcome: NodeOutcome) -> None:
        with self.ctx.session() as session:
            run = session.get(Run, loaded.run.id)
            step = session.scalars(
                select(StepRun).where(StepRun.run_id == loaded.run.id, StepRun.node_key == node.key)
            ).one()
            assert run is not None
            if step.status != ACTIVE:
                return
            self._settle_step(session, run, node, step, outcome)
            session.flush()
            session.refresh(step)
            loaded.steps[node.key] = _step_data(step)

    def _settle_step(
        self, session: Session, run: Run, node: NodeDef, step: StepRun, outcome: NodeOutcome
    ) -> None:
        now = self.ctx.clock.now()
        started = step.started_at or now
        duration = int((now - started).total_seconds() * 1000)
        state = {**dict(step.state or {}), **outcome.state}
        common = {"state": state}
        label = node.label

        if outcome.status == "succeeded" and node.output_schema:
            try:
                jsonschema.validate(outcome.outputs or {}, node.output_schema)
            except (jsonschema.ValidationError, jsonschema.SchemaError) as exc:
                outcome = NodeOutcome.fail(
                    FailureCategory.VALIDATION_FAILED,
                    f"Outputs do not match the output schema: {exc.message}",
                    retryable=False,
                    outputs=outcome.outputs,
                )

        if outcome.status == "running":
            if outcome.child_run_id:
                step.child_run_id = outcome.child_run_id
            step.state = state
            return
        if outcome.status == "waiting":
            approval_id = None
            if outcome.approval is not None:
                draft = outcome.approval
                approval = ApprovalService(self.ctx).request(
                    session,
                    workspace_id=run.workspace_id,
                    title=draft.title,
                    rationale=draft.rationale,
                    type=draft.type,
                    risk_level=draft.risk_level,
                    requested_action=draft.requested_action,
                    run_id=run.id,
                    step_run_id=step.id,
                    node_key=node.key,
                    resume_handler=APPROVAL_HANDLER,
                    requester=f"workflow:{(run.snapshot or {}).get('workflow', {}).get('name', run.title)}",
                    expires_in_seconds=draft.expires_in_seconds,
                    is_demo=run.is_demo,
                )
                approval_id = approval.id
            transition_step(
                session,
                step,
                RunStatus.WAITING_FOR_APPROVAL,
                approval_request_id=approval_id,
                outputs=outcome.outputs,
                **common,
            )
            self._event(
                session,
                run.id,
                f"'{label}' is waiting for approval",
                {"status": "waiting_for_approval", "approval_id": approval_id},
                node_key=node.key,
                step_run_id=step.id,
                severity=Severity.NOTICE,
            )
            return
        if outcome.status == "succeeded":
            transition_step(
                session,
                step,
                RunStatus.SUCCEEDED,
                outputs=outcome.outputs or {},
                finished_at=now,
                duration_ms=duration,
                failure_category=None,
                failure_message=None,
                **common,
            )
            self._event(
                session,
                run.id,
                f"Node '{label}' succeeded",
                {
                    "status": "succeeded",
                    "duration_ms": duration,
                    "output_keys": sorted((outcome.outputs or {}).keys())[:50],
                },
                node_key=node.key,
                step_run_id=step.id,
            )
            return
        if outcome.status == "cancelled":
            transition_step(
                session,
                step,
                RunStatus.CANCELLED,
                finished_at=now,
                duration_ms=duration,
                failure_category=FailureCategory.CANCELLED.value,
                failure_message=outcome.message or "Cancelled",
                **common,
            )
            self._event(
                session,
                run.id,
                f"Node '{label}' cancelled",
                {"status": "cancelled"},
                node_key=node.key,
                step_run_id=step.id,
                severity=Severity.WARNING,
            )
            return
        # failed / timed_out
        category = outcome.failure_category or FailureCategory.INTERNAL_ERROR
        message = (outcome.message or category.value)[:4000]
        target = RunStatus.TIMED_OUT if outcome.status == "timed_out" else RunStatus.FAILED
        can_retry = (
            outcome.retryable
            and self.nodes[node.type].inline
            and step.attempt_count < node.max_attempts
            and run.status not in (RunStatus.CANCELLING.value, RunStatus.CANCELLED.value)
        )
        transition_step(
            session,
            step,
            target,
            outputs=outcome.outputs,
            finished_at=now,
            duration_ms=duration,
            failure_category=category.value,
            failure_message=message,
            **common,
        )
        if can_retry:
            delay = node.retry_backoff_seconds * (2 ** max(0, step.attempt_count - 1))
            retry_at = now + timedelta(seconds=delay)
            transition_step(
                session,
                step,
                RunStatus.QUEUED,
                state={**state, "retry_at": _iso(retry_at), "last_failure": message},
            )
            self.recorder.append(
                session,
                run.id,
                [
                    EventDraft(
                        type=EventType.RETRY,
                        summary=f"'{label}' failed ({category.value}); retrying in {delay:g}s "
                        f"(attempt {step.attempt_count + 1} of {node.max_attempts})",
                        payload={
                            "failed_attempt": step.attempt_count,
                            "next_attempt": step.attempt_count + 1,
                            "max_attempts": node.max_attempts,
                            "delay_seconds": delay,
                            "failure_category": category.value,
                            "message": message,
                            "retry_at": _iso(retry_at),
                        },
                        severity=Severity.WARNING,
                        node_key=node.key,
                        step_run_id=step.id,
                    )
                ],
            )
            return
        fatal = failure_is_fatal_for(self, node, run)
        self._event(
            session,
            run.id,
            f"Node '{label}' {target.value.replace('_', ' ')}: {message}"[:480],
            {
                "status": target.value,
                "failure_category": category.value,
                "message": message,
                "fatal": fatal,
            },
            node_key=node.key,
            step_run_id=step.id,
            severity=Severity.ERROR if fatal else Severity.WARNING,
        )

    # ================================================================== failure / cancel

    def _fail_fast(self, loaded: Loaded, fatal_key: str) -> None:
        """A required node failed: stop everything else and fail the workflow."""
        children: list[str] = []
        with self.ctx.session() as session:
            run = session.get(Run, loaded.run.id)
            assert run is not None
            fatal = session.scalars(
                select(StepRun).where(StepRun.run_id == run.id, StepRun.node_key == fatal_key)
            ).one()
            children = self._stop_steps(session, run, "Stopped because a required node failed")
            category = fatal.failure_category or FailureCategory.DEPENDENCY_FAILED.value
            message = f"Node '{fatal.name}' failed: {fatal.failure_message or category}"
            self._fail_run_now(session, run, _category(category), message)
        self._cancel_children(children, "The workflow failed")

    def _fail_run_now(
        self, session: Session, run: Run, category: FailureCategory, message: str
    ) -> None:
        now = self.ctx.clock.now()
        if run.status == QUEUED:
            transition_run(
                session,
                run,
                RunStatus.FAILED,
                failure_category=category.value,
                failure_message=message,
                finished_at=now,
            )
        else:
            if run.status == RunStatus.PREPARING.value:
                transition_run(session, run, RunStatus.RUNNING)
            start = run.started_at or run.created_at
            transition_run(
                session,
                run,
                RunStatus.FAILED,
                failure_category=category.value,
                failure_message=message[:4000],
                finished_at=now,
                duration_ms=int((now - start).total_seconds() * 1000),
                outputs=self._run_outputs(session, run.id),
            )
        self.recorder.record(
            session,
            run.id,
            EventType.LIFECYCLE,
            "Workflow failed",
            {"status": "failed", "failure_category": category.value, "message": message},
            severity=Severity.ERROR,
        )

    def _stop_steps(self, session: Session, run: Run, reason: str) -> list[str]:
        """Skip queued steps and cancel running/waiting ones. Returns child runs to cancel."""
        now = self.ctx.clock.now()
        children: list[str] = []
        for step in session.scalars(select(StepRun).where(StepRun.run_id == run.id)):
            if step.status == QUEUED:
                transition_step(
                    session, step, RunStatus.SKIPPED, finished_at=now, failure_message=reason
                )
            elif step.status in (ACTIVE, WAITING, RunStatus.PREPARING.value):
                if step.child_run_id:
                    children.append(step.child_run_id)
                if step.approval_request_id:
                    self._cancel_approval(session, step.approval_request_id, reason)
                transition_step(
                    session,
                    step,
                    RunStatus.CANCELLED,
                    finished_at=now,
                    failure_category=FailureCategory.CANCELLED.value,
                    failure_message=reason,
                )
            else:
                continue
            self._event(
                session,
                run.id,
                f"'{step.name}' {'skipped' if step.status == 'skipped' else 'cancelled'}: {reason}",
                {"status": step.status, "reason": reason},
                node_key=step.node_key,
                step_run_id=step.id,
                severity=Severity.WARNING,
            )
        return children

    def _cancel_approval(self, session: Session, approval_id: str, reason: str) -> None:
        now = self.ctx.clock.now()
        result = session.execute(
            update(ApprovalRequest)
            .where(
                ApprovalRequest.id == approval_id,
                ApprovalRequest.status == ApprovalStatus.PENDING.value,
            )
            .values(
                status=ApprovalStatus.CANCELLED.value,
                resolved_at=now,
                reviewer="agenttape",
                reviewer_note=reason,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if (result.rowcount or 0) == 1:  # type: ignore[attr-defined]
            approval = session.get(ApprovalRequest, approval_id)
            record_audit(
                session,
                self.ctx.clock,
                action="approval.cancelled",
                resource_type="approval",
                resource_id=approval_id,
                actor="agenttape",
                run_id=approval.run_id if approval else None,
                workspace_id=approval.workspace_id if approval else None,
                details={"note": reason},
            )

    def _cancel_children(self, child_ids: list[str], reason: str) -> None:
        service = RunService(self.ctx)
        for child_id in child_ids:
            try:
                service.cancel_run(child_id, reason=reason, actor="workflow engine")
            except AgentTapeError:
                continue  # already finished or gone

    async def _cancel(self, run_id: str) -> None:
        """Propagate cancellation: pending approvals, running children, queued nodes."""

        def _do() -> tuple[list[str], bool]:
            with self.ctx.session() as session:
                run = session.get(Run, run_id)
                if run is None:
                    return [], False
                reason = run.cancel_reason or "Workflow cancelled"
                children = self._stop_steps(session, run, reason)
                finished = False
                if run.status == RunStatus.CANCELLING.value:
                    now = self.ctx.clock.now()
                    start = run.started_at or run.created_at
                    transition_run(
                        session,
                        run,
                        RunStatus.CANCELLED,
                        finished_at=now,
                        duration_ms=int((now - start).total_seconds() * 1000),
                        failure_category=FailureCategory.CANCELLED.value,
                        failure_message=reason,
                    )
                    self.recorder.record(
                        session,
                        run.id,
                        EventType.LIFECYCLE,
                        "Workflow cancelled",
                        {"status": "cancelled"},
                        severity=Severity.WARNING,
                    )
                    finished = True
                if children:
                    self._event(
                        session,
                        run.id,
                        f"Cancelling {len(children)} child run(s)",
                        {"child_run_ids": children},
                        severity=Severity.WARNING,
                    )
                return children, finished

        children, finished = await asyncio.to_thread(_do)
        await asyncio.to_thread(
            self._cancel_children, children, "The parent workflow was cancelled"
        )
        if finished:
            await self._after_terminal(run_id, RunStatus.CANCELLED.value)

    # ================================================================== finalize

    def _run_outputs(self, session: Session, run_id: str) -> dict[str, Any]:
        steps = session.scalars(
            select(StepRun).where(StepRun.run_id == run_id).order_by(StepRun.position)
        ).all()
        result: Any = None
        for s in steps:
            if s.node_type == NodeType.COLLECT.value and s.status == "succeeded":
                result = s.outputs
        return {
            "nodes": {s.node_key: {"status": s.status, "outputs": s.outputs} for s in steps},
            "result": result,
        }

    def _finalize(self, loaded: Loaded) -> tuple[str, datetime | None, bool]:
        with self.ctx.session() as session:
            run = session.get(Run, loaded.run.id)
            assert run is not None
            if run.status in (
                "succeeded",
                "failed",
                "timed_out",
                "cancelled",
                "cancelling",
                "skipped",
            ):
                return run.status, None, False
            steps = list(session.scalars(select(StepRun).where(StepRun.run_id == run.id)))
            now = self.ctx.clock.now()
            statuses = [s.status for s in steps]
            if all(s in TERMINAL_STEP_STATES for s in statuses):
                self._ensure_running(session, run)
                start = run.started_at or run.created_at
                duration = int((now - start).total_seconds() * 1000)
                failed = [s.node_key for s in steps if s.status in ("failed", "timed_out")]
                transition_run(
                    session,
                    run,
                    RunStatus.SUCCEEDED,
                    finished_at=now,
                    duration_ms=duration,
                    outputs=self._run_outputs(session, run.id),
                    failure_category=None,
                    failure_message=None,
                )
                self.recorder.record(
                    session,
                    run.id,
                    EventType.LIFECYCLE,
                    "Workflow succeeded"
                    + (f" ({len(failed)} optional node(s) failed)" if failed else ""),
                    {"status": "succeeded", "duration_ms": duration, "optional_failures": failed},
                )
                return "succeeded", None, True
            if ACTIVE in statuses:
                self._ensure_running(session, run)
                return "running", None, False
            if WAITING in statuses:
                if run.status == ACTIVE:
                    waiting = [s.name for s in steps if s.status == WAITING]
                    transition_run(session, run, RunStatus.WAITING_FOR_APPROVAL)
                    self.recorder.record(
                        session,
                        run.id,
                        EventType.LIFECYCLE,
                        "Workflow paused: waiting for approval (" + ", ".join(waiting) + ")",
                        {"status": "waiting_for_approval", "nodes": waiting},
                        severity=Severity.NOTICE,
                    )
                    return "waiting_for_approval", None, True
                return run.status, None, False
            # Only queued steps remain: they are waiting for a retry backoff.
            self._ensure_running(session, run)
            retry_times = [
                t
                for t in (
                    _parse((s.state or {}).get("retry_at")) for s in steps if s.status == QUEUED
                )
                if t
            ]
            wake = min(retry_times) if retry_times else now + timedelta(seconds=1)
            return "running", wake, False

    def _ensure_running(self, session: Session, run: Run) -> None:
        if run.status == WAITING:
            transition_run(session, run, RunStatus.QUEUED, queued_at=self.ctx.clock.now())
        if run.status == QUEUED:
            transition_run(session, run, RunStatus.PREPARING)
        if run.status == RunStatus.PREPARING.value:
            transition_run(session, run, RunStatus.RUNNING)

    # ================================================================== helpers

    def _event(
        self,
        session: Session,
        run_id: str,
        summary: str,
        payload: dict[str, Any],
        *,
        node_key: str | None = None,
        step_run_id: str | None = None,
        severity: Severity = Severity.INFO,
    ) -> None:
        self.recorder.append(
            session,
            run_id,
            [
                EventDraft(
                    type=EventType.STEP,
                    summary=summary,
                    payload=payload,
                    node_key=node_key,
                    step_run_id=step_run_id,
                    severity=severity,
                    source="workflow",
                )
            ],
        )

    async def _watch(self, run_id: str, claim_id: str | None, cancel: CancellationToken) -> None:
        """Poll for cancellation and keep the per-run lease and queue lease alive."""
        ttl = max(5.0, self.ctx.settings.lease_seconds)
        last_renew = time.monotonic()

        def _check() -> str | None:
            with self.ctx.session() as session:
                run = session.get(Run, run_id)
                return run.status if run is not None else None

        while True:
            await asyncio.sleep(WATCH_SECONDS)
            status = await asyncio.to_thread(_check)
            if status in (None, RunStatus.CANCELLING.value, RunStatus.CANCELLED.value):
                cancel.cancel(FailureCategory.CANCELLED, "Workflow cancelled")
            if time.monotonic() - last_renew >= ttl / 3:
                last_renew = time.monotonic()
                await asyncio.to_thread(
                    self.ctx.leases.acquire, lease_name(run_id), self.worker_id, ttl
                )
                if claim_id:
                    await asyncio.to_thread(
                        self.ctx.queue.renew,
                        claim_id,
                        self.worker_id,
                        self.ctx.settings.lease_seconds,
                    )

    @staticmethod
    async def _forward_cancel(cancel: CancellationToken, tokens: list[CancellationToken]) -> None:
        await cancel.wait()
        for token in list(tokens):
            token.cancel(FailureCategory.CANCELLED, cancel.message or "Workflow cancelled")


def failure_is_fatal_for(engine: WorkflowEngine, node: NodeDef, run: Run) -> bool:
    section = (run.snapshot or {}).get("workflow") or {}
    try:
        definition = WorkflowDefinition.model_validate(section.get("definition") or {})
    except Exception:
        return True
    return failure_is_fatal(definition, node)
