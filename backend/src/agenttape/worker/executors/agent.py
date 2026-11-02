"""Executor for ``kind == "agent"`` runs: one adapter execution per attempt.

Lifecycle of a claimed run (all status changes go through race-safe transitions):

1. prepare  - queued -> preparing; create attempt N (or reopen the attempt that is
              waiting for approval, to resume from its checkpoint);
2. running  - preparing -> running; the adapter executes with watchdogs for wall-clock
              timeout, idle timeout, cancellation requests, and lease renewal;
3. evaluate - configured evaluators run on the attempt; a failed *required* evaluation
              fails the run (``evaluation_failed``);
4. finalize - succeeded / waiting_for_approval / failed / timed_out / cancelled, or back
              to queued with exponential backoff when the failure category is retryable
              and attempts remain.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import time
import traceback
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select

from agenttape.adapters.base import (
    AdapterResult,
    AttemptInfo,
    CancellationToken,
    EventEmitter,
    ExecutionContext,
    RunInfo,
)
from agenttape.application.context import AppContext
from agenttape.application.events import EventRecorder
from agenttape.application.ports import ClaimedItem
from agenttape.application.services.approvals import ApprovalService
from agenttape.application.services.evaluations import EvaluationService
from agenttape.application.services.runs import RunService
from agenttape.application.transitions import (
    ConcurrentUpdateError,
    transition_attempt,
    transition_run,
)
from agenttape.context_evidence.pack import prepare_attempt_context
from agenttape.domain.enums import (
    EvaluationStatus,
    EventType,
    FailureCategory,
    RunStatus,
    Severity,
)
from agenttape.domain.events import EventDraft
from agenttape.domain.prompts import RenderMode, VariableDefinition, render_template
from agenttape.domain.retry import RetryPolicy
from agenttape.domain.usage import merge_usage
from agenttape.evaluations.base import EvaluationContext, EvaluationSpec
from agenttape.file_evidence.collector import start_file_evidence
from agenttape.herdr.sessions import RuntimeStore, reattach_or_none
from agenttape.infrastructure.db.models import Run, RunAttempt, SecretReference
from agenttape.security.attempt_redactors import configured_redactor
from agenttape.security.capture import capture_api_base
from agenttape.security.redaction import Redactor
from agenttape.security.secrets import EnvSecretResolver, SecretLocator, SecretUnavailableError
from agenttape.worker import hooks
from agenttape.worker.recorders import DbArtifactWriter, DbFileChangeRecorder
from agenttape.worker.registry import RunExecutor, WorkerRuntime, register_executor
from agenttape.worker.sink import BatchingEventSink

CANCEL_POLL_SECONDS = 1.0
TIMEOUT_CATEGORIES = (FailureCategory.TIMEOUT, FailureCategory.IDLE_TIMEOUT)


@dataclass
class Prepared:
    run_id: str
    workspace_id: str
    title: str
    kind: str
    source: str
    project_id: str | None
    is_demo: bool
    snapshot: dict[str, Any]
    options: dict[str, Any]
    inputs: dict[str, Any]
    rendered_prompt: str | None
    adapter_type: str
    attempt_id: str
    attempt_number: int
    max_attempts: int
    resume_state: dict[str, Any] | None
    log_dir: Path
    secret_refs: dict[str, SecretLocator]


def base_redactor(ctx: AppContext) -> Redactor:
    """Redactor seeded with the values of ``AGENTTAPE_REDACT_ENV`` variables."""
    return configured_redactor(ctx.settings.redact_env)


@register_executor
class AgentRunExecutor(RunExecutor):
    kind = "agent"

    async def execute(self, runtime: WorkerRuntime, claim: ClaimedItem) -> None:
        ctx = runtime.ctx
        try:
            prepared = await asyncio.to_thread(self._prepare, runtime, claim)
        except ConcurrentUpdateError:
            prepared = None
        if prepared is None:
            await asyncio.to_thread(ctx.queue.complete, claim.id, runtime.worker_id)
            return
        try:
            await self._run_attempt(runtime, claim, prepared)
        finally:
            await asyncio.to_thread(ctx.queue.complete, claim.id, runtime.worker_id)

    # ------------------------------------------------------------------ prepare

    def _prepare(self, runtime: WorkerRuntime, claim: ClaimedItem) -> Prepared | None:
        ctx = runtime.ctx
        recorder = EventRecorder(ctx.settings, ctx.clock)
        with ctx.session() as session:
            run = session.get(Run, claim.run_id) if claim.run_id else None
            if run is None or run.status != RunStatus.QUEUED.value:
                return None
            now = ctx.clock.now()
            lease_until = now + timedelta(seconds=ctx.settings.lease_seconds)
            waiting = session.scalars(
                select(RunAttempt)
                .where(
                    RunAttempt.run_id == run.id,
                    RunAttempt.status == RunStatus.WAITING_FOR_APPROVAL.value,
                )
                .order_by(RunAttempt.attempt_number.desc())
                .limit(1)
            ).first()
            log_dir = ctx.settings.logs_dir / run.id
            if waiting is not None:
                attempt = waiting
                attempt.worker_id = runtime.worker_id
                attempt.heartbeat_at = now
                attempt.lease_expires_at = lease_until
                resume_state = dict(attempt.checkpoint or {})
                attempt_count = run.attempt_count
            else:
                attempt_count = run.attempt_count + 1
                attempt = RunAttempt(
                    run_id=run.id,
                    attempt_number=attempt_count,
                    status=RunStatus.PREPARING.value,
                    worker_id=runtime.worker_id,
                    created_at=now,
                    started_at=now,
                    heartbeat_at=now,
                    lease_expires_at=lease_until,
                    log_dir=str(log_dir),
                )
                session.add(attempt)
                resume_state = None
            transition_run(
                session,
                run,
                RunStatus.PREPARING,
                started_at=run.started_at or now,
                attempt_count=attempt_count,
            )
            session.flush()
            recorder.record(
                session,
                run.id,
                EventType.LIFECYCLE,
                (
                    f"Reattaching attempt {attempt.attempt_number} to its Herdr session"
                    if resume_state is not None and "herdr_reattach" in resume_state
                    else f"Resuming attempt {attempt.attempt_number} after approval"
                    if resume_state is not None
                    else f"Attempt {attempt.attempt_number} of {run.max_attempts} preparing"
                ),
                {
                    "status": "preparing",
                    "attempt": attempt.attempt_number,
                    "worker_id": runtime.worker_id,
                    "resumed": resume_state is not None,
                },
                attempt_id=attempt.id,
            )
            refs = {
                r.name: SecretLocator(provider=r.provider, locator=r.locator)
                for r in session.scalars(
                    select(SecretReference).where(SecretReference.workspace_id == run.workspace_id)
                )
            }
            snapshot = dict(run.snapshot or {})
            return Prepared(
                run_id=run.id,
                workspace_id=run.workspace_id,
                title=run.title,
                kind=run.kind,
                source=run.source,
                project_id=run.project_id,
                is_demo=run.is_demo,
                snapshot=snapshot,
                options=dict(run.options or {}),
                inputs=dict(run.inputs or {}),
                rendered_prompt=run.rendered_prompt,
                adapter_type=str(run.adapter_type),
                attempt_id=attempt.id,
                attempt_number=attempt.attempt_number,
                max_attempts=run.max_attempts,
                resume_state=resume_state,
                log_dir=log_dir,
                secret_refs=refs,
            )

    # ------------------------------------------------------------------ run

    async def _run_attempt(self, runtime: WorkerRuntime, claim: ClaimedItem, p: Prepared) -> None:
        # The attempt's redactor collects every secret value the attempt resolves. It is
        # published (in memory only) while the attempt runs, so events recorded for this
        # attempt outside the sink (Claude Code hooks of a Herdr-hosted session, MCP
        # findings) are redacted with the same values.
        redactor = base_redactor(runtime.ctx)
        with runtime.ctx.attempt_redactors.registered(p.attempt_id, redactor):
            await self._run_registered_attempt(runtime, claim, p, redactor)

    async def _run_registered_attempt(
        self, runtime: WorkerRuntime, claim: ClaimedItem, p: Prepared, redactor: Redactor
    ) -> None:
        ctx = runtime.ctx
        settings = ctx.settings
        started = time.monotonic()
        resolver = EnvSecretResolver(references=p.secret_refs)
        sink = BatchingEventSink(
            run_id=p.run_id,
            recorder=EventRecorder(settings, ctx.clock, artifact_store=ctx.artifact_store),
            session_factory=ctx.session_factory,
            redactor=redactor,
            started_monotonic=started,
        )
        await sink.start()
        last_activity = [time.monotonic()]

        def touch() -> None:
            last_activity[0] = time.monotonic()

        emitter = EventEmitter(
            sink.emit, source=p.adapter_type, attempt_id=p.attempt_id, on_activity=touch
        )
        system = EventEmitter(sink.emit, source="agenttape", attempt_id=p.attempt_id)
        cancel = CancellationToken()

        def resolve_secret(name: str) -> str:
            value = resolver.resolve(name)
            redactor.add_secrets([value])
            return value

        prompt_snap = dict(p.snapshot.get("prompt") or {})

        def resolved_prompt() -> str | None:
            if not prompt_snap:
                return None
            defs = [
                VariableDefinition.model_validate(v) for v in prompt_snap.get("variables") or []
            ]
            return render_template(
                str(prompt_snap.get("body", "")),
                defs,
                p.inputs,
                mode=RenderMode.RESOLVE,
                resolve_secret=resolve_secret,
            ).text

        project_path_raw = (p.snapshot.get("project") or {}).get("repo_path")
        result: AdapterResult
        exec_ctx: ExecutionContext | None = None
        watchdogs: list[asyncio.Task[None]] = []
        # A watchdog that stopped the attempt records why in a task of its own, so tearing
        # the watchdogs down can never drop that event (see _start_watchdogs).
        notices: list[asyncio.Task[None]] = []
        try:
            workdir = await hooks.workdir_preparer()(
                ctx,
                hooks.WorkdirRequest(
                    run_id=p.run_id,
                    attempt_number=p.attempt_number,
                    snapshot=p.snapshot,
                    project_path=Path(project_path_raw) if project_path_raw else None,
                    data_dir=settings.data_dir,
                ),
            )
            for warning in workdir.warnings:
                await system.event(EventType.WARNING, warning, {}, severity=Severity.WARNING)
            p.log_dir.mkdir(parents=True, exist_ok=True)
            # Context pack (M2): assembled before the agent starts; approved project
            # lessons are appended to the prompt and recorded in the pack as injected.
            lessons = await asyncio.to_thread(
                prepare_attempt_context, ctx, p.run_id, p.attempt_id, workdir.path
            )

            def adapter_prompt() -> str | None:
                text = resolved_prompt()
                return text + lessons if text and lessons else text

            exec_ctx = ExecutionContext(
                run=RunInfo(
                    id=p.run_id,
                    title=p.title,
                    kind=p.kind,
                    source=p.source,
                    project_id=p.project_id,
                    workspace_id=p.workspace_id,
                    is_demo=p.is_demo,
                    snapshot=p.snapshot,
                ),
                attempt=AttemptInfo(
                    id=p.attempt_id, number=p.attempt_number, max_attempts=p.max_attempts
                ),
                adapter_type=p.adapter_type,
                profile=dict((p.snapshot.get("agent_profile") or {}).get("config") or {}),
                prompt=p.rendered_prompt + lessons
                if p.rendered_prompt and lessons
                else p.rendered_prompt,
                inputs=p.inputs,
                workdir=workdir.path,
                events=emitter,
                cancel=cancel,
                artifacts=DbArtifactWriter(
                    ctx,
                    run_id=p.run_id,
                    attempt_id=p.attempt_id,
                    step_run_id=None,
                    redactor=redactor,
                    events=emitter,
                ),
                files=DbFileChangeRecorder(
                    ctx, run_id=p.run_id, attempt_id=p.attempt_id, step_run_id=None, events=emitter
                ),
                resume_state=p.resume_state,
                data_dir=settings.data_dir,
                log_dir=p.log_dir,
                resolve_secret=resolve_secret,
                resolved_prompt=adapter_prompt,
                redact=redactor.redact_text,
                limits={
                    "cancel_grace_seconds": settings.cancel_grace_seconds,
                    "max_raw_log_bytes": settings.max_raw_log_bytes,
                    "max_artifact_bytes": settings.max_artifact_bytes,
                    "demo_step_delay_ms": settings.demo_step_delay_ms,
                    "timeout_seconds": p.options.get("timeout_seconds"),
                    "idle_timeout_seconds": p.options.get("idle_timeout_seconds"),
                    "capture_api_base": capture_api_base(settings),
                },
                runtime_store=RuntimeStore(ctx, p.run_id, p.attempt_id),
            )
            proceed = await asyncio.to_thread(
                self._mark_running, ctx, p, str(workdir.path) if workdir.path else None
            )
            if not proceed:
                cancel.cancel(FailureCategory.CANCELLED, "Cancelled before the agent started")
            adapter = ctx.adapters.get(p.adapter_type)
            watchdogs = self._start_watchdogs(
                runtime, claim, p, cancel, system, last_activity, notices
            )
            # File evidence (M2): the start snapshot is taken and the watcher is live
            # before the agent starts; the collector then runs for the attempt.
            collector = await start_file_evidence(ctx, exec_ctx, system)
            if collector is not None:
                watchdogs.append(collector)
            watchdogs += [
                asyncio.create_task(monitor(ctx, exec_ctx, system))
                for monitor in hooks.attempt_monitors()
            ]
            if cancel.cancelled:
                result = AdapterResult.failure(
                    cancel.reason or FailureCategory.CANCELLED, cancel.message or ""
                )
            else:
                result = await adapter.execute(exec_ctx)
        except SecretUnavailableError as exc:
            result = AdapterResult.failure(FailureCategory.CONFIG_ERROR, exc.message)
            await system.event(
                EventType.ERROR,
                exc.message,
                {"secret": exc.details.get("secret")},
                severity=Severity.ERROR,
            )
        except asyncio.CancelledError:
            cancel.cancel(FailureCategory.INTERRUPTED, "Worker is shutting down")
            result = AdapterResult.failure(FailureCategory.INTERRUPTED, "Worker is shutting down")
            await self._stop(watchdogs, notices)
            with contextlib.suppress(Exception):
                await system.event(
                    EventType.ERROR,
                    "Worker stopped during the attempt",
                    {},
                    severity=Severity.ERROR,
                )
                await sink.close()
                # A Herdr-hosted agent keeps running: park the attempt for reattachment.
                if not await asyncio.to_thread(
                    reattach_or_none, ctx, p.attempt_id, "worker_shutdown"
                ):
                    await asyncio.to_thread(self._finalize, runtime, p, result, started)
            raise
        except Exception as exc:
            tb = redactor.redact_text("".join(traceback.format_exception(exc))[-4000:])
            result = AdapterResult.failure(
                FailureCategory.ADAPTER_ERROR, f"{type(exc).__name__}: {exc}"
            )
            await system.event(
                EventType.ERROR,
                f"Adapter crashed: {type(exc).__name__}: {exc}",
                {"traceback": tb},
                severity=Severity.ERROR,
            )
        finally:
            await self._stop(watchdogs, notices)

        if cancel.cancelled and (
            result.outcome != "failed" or result.failure_category != cancel.reason
        ):
            result = AdapterResult.failure(
                cancel.reason or FailureCategory.CANCELLED,
                cancel.message or "Stopped",
                exit_code=result.exit_code,
                usage=result.usage,
                cost=result.cost,
                outputs=result.outputs,
            )

        if exec_ctx is not None:
            for hook in hooks.after_attempt_hooks():
                try:
                    result = await hook(ctx, exec_ctx, result)
                except Exception as exc:
                    await system.event(
                        EventType.ERROR,
                        f"after-attempt hook failed: {exc}",
                        {"hook": getattr(hook, "__name__", "?")},
                        severity=Severity.ERROR,
                    )

        if result.outcome in ("succeeded", "failed") and not cancel.cancelled:
            result = await self._evaluate(
                ctx, p, result, exec_ctx, int((time.monotonic() - started) * 1000)
            )

        await sink.close()
        status = await asyncio.to_thread(self._finalize, runtime, p, result, started)
        await hooks.fire_run_terminal(ctx, p.run_id, status)

    def _mark_running(self, ctx: AppContext, p: Prepared, workdir: str | None) -> bool:
        recorder = EventRecorder(ctx.settings, ctx.clock)
        with ctx.session() as session:
            run = session.get(Run, p.run_id)
            attempt = session.get(RunAttempt, p.attempt_id)
            assert run is not None and attempt is not None
            attempt.workdir = workdir
            session.flush()  # persist before transitions expire the instance
            if run.status != RunStatus.PREPARING.value:
                return False
            transition_run(session, run, RunStatus.RUNNING)
            transition_attempt(session, attempt, RunStatus.RUNNING)
            recorder.record(
                session,
                p.run_id,
                EventType.LIFECYCLE,
                f"Attempt {p.attempt_number} running",
                {
                    "status": "running",
                    "attempt": p.attempt_number,
                    "workdir": workdir,
                    "adapter": p.adapter_type,
                },
                attempt_id=p.attempt_id,
            )
            return True

    # ------------------------------------------------------------------ watchdogs

    def _start_watchdogs(
        self,
        runtime: WorkerRuntime,
        claim: ClaimedItem,
        p: Prepared,
        cancel: CancellationToken,
        system: EventEmitter,
        last_activity: list[float],
        notices: list[asyncio.Task[None]],
    ) -> list[asyncio.Task[None]]:
        ctx = runtime.ctx

        async def notice(summary: str, payload: dict[str, Any]) -> None:
            # Once this watchdog has cancelled the attempt, the adapter can return and
            # teardown can cancel the watchdog while this write still waits for the sink's
            # lock or a database thread (a busy machine). The write runs as its own task,
            # shielded here and awaited by _stop, so the TIMEOUT event is never lost.
            task = asyncio.ensure_future(
                system.event(EventType.TIMEOUT, summary, payload, severity=Severity.ERROR)
            )
            notices.append(task)
            await asyncio.shield(task)

        timeout = float(p.options.get("timeout_seconds") or ctx.settings.default_timeout_seconds)
        idle = float(
            p.options.get("idle_timeout_seconds") or ctx.settings.default_idle_timeout_seconds
        )

        async def wall_clock() -> None:
            if await cancel.sleep(timeout):
                return
            if cancel.cancel(
                FailureCategory.TIMEOUT, f"Exceeded the wall-clock limit of {timeout:g}s"
            ):
                await notice(
                    f"Timed out after {timeout:g}s; stopping the agent",
                    {"limit_seconds": timeout, "kind": "wall_clock"},
                )

        async def idle_watch() -> None:
            while not cancel.cancelled:
                if await cancel.sleep(min(1.0, idle)):
                    return
                silent = time.monotonic() - last_activity[0]
                if silent >= idle:
                    if cancel.cancel(FailureCategory.IDLE_TIMEOUT, f"No activity for {idle:g}s"):
                        await notice(
                            f"No output for {idle:g}s; stopping the agent",
                            {"limit_seconds": idle, "kind": "idle"},
                        )
                    return

        def _cancel_requested() -> str | None:
            with ctx.session() as session:
                row = session.execute(
                    select(Run.status, Run.cancel_reason).where(Run.id == p.run_id)
                ).one_or_none()
                if row is None:
                    return "Run was deleted"
                if row[0] == RunStatus.CANCELLING.value:
                    return str(row[1] or "Cancellation requested")
                return None

        async def cancel_poll() -> None:
            while not cancel.cancelled:
                if await cancel.sleep(CANCEL_POLL_SECONDS):
                    return
                reason = await asyncio.to_thread(_cancel_requested)
                if reason is not None:
                    cancel.cancel(FailureCategory.CANCELLED, reason)
                    return

        def _renew() -> bool:
            ok = ctx.queue.renew(claim.id, runtime.worker_id, ctx.settings.lease_seconds)
            with ctx.session() as session:
                attempt = session.get(RunAttempt, p.attempt_id)
                if attempt is not None:
                    now = ctx.clock.now()
                    attempt.heartbeat_at = now
                    attempt.lease_expires_at = now + timedelta(seconds=ctx.settings.lease_seconds)
            return ok

        async def renew_lease() -> None:
            interval = max(0.2, ctx.settings.lease_seconds / 3)
            while not cancel.cancelled:
                if await cancel.sleep(interval):
                    return
                if not await asyncio.to_thread(_renew):
                    cancel.cancel(FailureCategory.INTERRUPTED, "Lost the queue lease for this run")
                    return

        return [
            asyncio.create_task(wall_clock()),
            asyncio.create_task(idle_watch()),
            asyncio.create_task(cancel_poll()),
            asyncio.create_task(renew_lease()),
        ]

    @staticmethod
    async def _stop(
        tasks: list[asyncio.Task[None]], notices: list[asyncio.Task[None]] | None = None
    ) -> None:
        """Cancel the watchdogs, then wait for any notice a watchdog already began writing.

        ``asyncio.wait`` never cancels what it waits on, so if this executor is itself
        cancelled here (worker shutdown) a notice still finishes writing, and the
        cancellation propagates instead of being swallowed. A watchdog's or
        notice's own failure is retrieved and ignored, as before."""
        for t in tasks:
            t.cancel()
        waiting = [*tasks, *(notices or [])]
        if waiting:
            done, _pending = await asyncio.wait(waiting)
            for t in done:
                if not t.cancelled():
                    t.exception()
        tasks.clear()
        if notices is not None:
            notices.clear()

    # ------------------------------------------------------------------ evaluate

    async def _evaluate(
        self,
        ctx: AppContext,
        p: Prepared,
        result: AdapterResult,
        exec_ctx: ExecutionContext | None,
        duration_ms: int,
    ) -> AdapterResult:
        specs = [EvaluationSpec.from_dict(e) for e in p.options.get("evaluators") or []]
        if not specs:
            return result
        service = EvaluationService(ctx)
        loaders = service.loaders(p.run_id)
        ectx = EvaluationContext(
            run_id=p.run_id,
            attempt_id=p.attempt_id,
            step_run_id=None,
            exit_code=result.exit_code,
            outcome=result.outcome,
            outputs=result.outputs,
            workdir=exec_ctx.workdir if exec_ctx else None,
            duration_ms=duration_ms,
            usage=result.usage,
            **loaders,
        )
        failed_required: list[str] = []
        for spec in specs:
            outcome, took, version = await service.evaluate(spec, ectx)
            await asyncio.to_thread(
                functools.partial(
                    service.persist,
                    run_id=p.run_id,
                    attempt_id=p.attempt_id,
                    step_run_id=None,
                    spec=spec,
                    outcome=outcome,
                    duration_ms=took,
                    evaluator_version=version,
                )
            )
            if spec.required and outcome.status in (EvaluationStatus.FAIL, EvaluationStatus.ERROR):
                failed_required.append(spec.name or spec.type)
        if failed_required and result.outcome == "succeeded":
            return AdapterResult.failure(
                FailureCategory.EVALUATION_FAILED,
                "Required evaluation failed: " + ", ".join(failed_required),
                exit_code=result.exit_code,
                outputs=result.outputs,
                usage=result.usage,
                cost=result.cost,
            )
        return result

    # ------------------------------------------------------------------ finalize

    def _finalize(
        self, runtime: WorkerRuntime, p: Prepared, result: AdapterResult, started: float
    ) -> str:
        last_error: Exception | None = None
        for _ in range(3):
            try:
                return self._finalize_once(runtime, p, result, started)
            except ConcurrentUpdateError as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    def _finalize_once(
        self, runtime: WorkerRuntime, p: Prepared, result: AdapterResult, started: float
    ) -> str:
        ctx = runtime.ctx
        recorder = EventRecorder(ctx.settings, ctx.clock)
        with ctx.session() as session:
            run = session.get(Run, p.run_id)
            attempt = session.get(RunAttempt, p.attempt_id)
            if run is None or attempt is None:
                return "deleted"
            now = ctx.clock.now()
            attempt_ms = int((time.monotonic() - started) * 1000)
            if attempt.status in (
                RunStatus.SUCCEEDED.value,
                RunStatus.FAILED.value,
                RunStatus.CANCELLED.value,
                RunStatus.TIMED_OUT.value,
            ):
                # Another process (reconciliation) already settled this attempt.
                return run.status
            run_usage = merge_usage(run.usage, result.usage)
            attempt_usage = merge_usage(attempt.usage, result.usage)
            run_ms = int((now - (run.started_at or run.created_at)).total_seconds() * 1000)
            common: dict[str, Any] = {
                "exit_code": result.exit_code,
                "outputs": result.outputs or None,
                "usage": attempt_usage,
                "cost": result.cost,
                "stdout_bytes": _log_size(p, "stdout"),
                "stderr_bytes": _log_size(p, "stderr"),
            }
            drafts: list[EventDraft] = []

            if (
                result.outcome == "needs_approval"
                and result.approval is not None
                and run.status == RunStatus.RUNNING.value
            ):
                transition_attempt(
                    session,
                    attempt,
                    RunStatus.WAITING_FOR_APPROVAL,
                    checkpoint=result.checkpoint or {},
                    heartbeat_at=now,
                    lease_expires_at=None,
                    **common,
                )
                transition_run(session, run, RunStatus.WAITING_FOR_APPROVAL, usage=run_usage)
                spec = result.approval
                ApprovalService(ctx).request(
                    session,
                    workspace_id=p.workspace_id,
                    title=spec.title,
                    rationale=spec.rationale,
                    type=spec.type,
                    risk_level=spec.risk_level,
                    requested_action=spec.requested_action,
                    run_id=p.run_id,
                    attempt_id=p.attempt_id,
                    resume_handler="run",
                    requester=p.adapter_type,
                    expires_in_seconds=spec.expires_in_seconds,
                    is_demo=p.is_demo,
                )
                drafts.append(
                    _life(
                        "Waiting for approval", "waiting_for_approval", p, severity=Severity.NOTICE
                    )
                )
                recorder.append(session, p.run_id, drafts)
                return RunStatus.WAITING_FOR_APPROVAL.value

            if result.outcome == "succeeded":
                transition_attempt(
                    session,
                    attempt,
                    RunStatus.SUCCEEDED,
                    finished_at=now,
                    duration_ms=attempt_ms,
                    lease_expires_at=None,
                    **common,
                )
                transition_run(
                    session,
                    run,
                    RunStatus.SUCCEEDED,
                    finished_at=now,
                    duration_ms=run_ms,
                    outputs=result.outputs or None,
                    usage=run_usage,
                    cost=result.cost,
                    failure_category=None,
                    failure_message=None,
                )
                drafts.append(_life("Run succeeded", "succeeded", p, duration_ms=run_ms))
                recorder.append(session, p.run_id, drafts)
                return RunStatus.SUCCEEDED.value

            category = result.failure_category or FailureCategory.ADAPTER_ERROR
            message = (result.message or category.value)[:4000]
            if result.outcome == "needs_approval":
                category, message = FailureCategory.CANCELLED, "Cancelled while requesting approval"
            attempt_target = (
                RunStatus.TIMED_OUT
                if category in TIMEOUT_CATEGORIES
                else RunStatus.CANCELLED
                if category == FailureCategory.CANCELLED
                else RunStatus.FAILED
            )
            transition_attempt(
                session,
                attempt,
                attempt_target,
                finished_at=now,
                duration_ms=attempt_ms,
                failure_category=category.value,
                failure_message=message,
                lease_expires_at=None,
                **common,
            )
            policy = RetryPolicy.from_config(
                p.options.get("retry"),
                default_max_attempts=run.max_attempts,
                default_base=ctx.settings.retry_backoff_base_seconds,
                default_max=ctx.settings.retry_backoff_max_seconds,
            )
            policy = RetryPolicy(
                max_attempts=run.max_attempts,
                backoff_base_seconds=policy.backoff_base_seconds,
                backoff_max_seconds=policy.backoff_max_seconds,
                jitter_ratio=policy.jitter_ratio,
                retry_on=policy.retry_on,
            )
            cancel_requested = (
                run.cancel_requested_at is not None or run.status == RunStatus.CANCELLING.value
            )
            if category == FailureCategory.CANCELLED or cancel_requested:
                transition_run(
                    session,
                    run,
                    RunStatus.CANCELLED,
                    finished_at=now,
                    duration_ms=run_ms,
                    usage=run_usage,
                    failure_category=FailureCategory.CANCELLED.value,
                    failure_message=run.cancel_reason or message,
                )
                drafts.append(_life("Run cancelled", "cancelled", p, severity=Severity.WARNING))
                recorder.append(session, p.run_id, drafts)
                return RunStatus.CANCELLED.value
            if policy.should_retry(category, attempt.attempt_number):
                delay = policy.backoff_seconds(attempt.attempt_number)
                when = now + timedelta(seconds=delay)
                attempt.retry_scheduled_at = when
                transition_run(session, run, RunStatus.QUEUED, queued_at=now, usage=run_usage)
                drafts.append(
                    EventDraft(
                        type=EventType.RETRY,
                        summary=(
                            f"Attempt {attempt.attempt_number} failed ({category.value}); "
                            f"retrying in {delay:g}s (attempt {attempt.attempt_number + 1} of {run.max_attempts})"
                        ),
                        payload={
                            "failed_attempt": attempt.attempt_number,
                            "next_attempt": attempt.attempt_number + 1,
                            "max_attempts": run.max_attempts,
                            "delay_seconds": delay,
                            "failure_category": category.value,
                            "message": message,
                            "retry_at": when.isoformat(),
                        },
                        severity=Severity.WARNING,
                        attempt_id=p.attempt_id,
                    )
                )
                drafts.append(_life("Run queued for retry", "queued", p))
                recorder.append(session, p.run_id, drafts)
                RunService(ctx).enqueue(session, run, available_at=when)
                return RunStatus.QUEUED.value
            run_target = RunStatus.TIMED_OUT if category in TIMEOUT_CATEGORIES else RunStatus.FAILED
            transition_run(
                session,
                run,
                run_target,
                finished_at=now,
                duration_ms=run_ms,
                usage=run_usage,
                failure_category=category.value,
                failure_message=message,
                outputs=result.outputs or None,
            )
            drafts.append(
                _life(
                    "Run timed out" if run_target == RunStatus.TIMED_OUT else "Run failed",
                    run_target.value,
                    p,
                    severity=Severity.ERROR,
                    failure_category=category.value,
                    message=message,
                )
            )
            recorder.append(session, p.run_id, drafts)
            return run_target.value


def _log_size(p: Prepared, stream: str) -> int:
    try:
        return (p.log_dir / f"attempt-{p.attempt_number}.{stream}.log").stat().st_size
    except OSError:
        return 0


def _life(
    summary: str, status: str, p: Prepared, *, severity: Severity = Severity.INFO, **payload: Any
) -> EventDraft:
    return EventDraft(
        type=EventType.LIFECYCLE,
        summary=summary,
        payload={"status": status, "attempt": p.attempt_number, **payload},
        severity=severity,
        attempt_id=p.attempt_id,
    )
