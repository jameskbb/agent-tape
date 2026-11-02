from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from agenttape.adapters.base import (
    AdapterResult,
    AgentAdapter,
    DiscoveryResult,
    ExecutionContext,
    ProfileIssue,
)
from agenttape.adapters.process import CommandSpec, run_process
from agenttape.application.context import AppContext
from agenttape.application.schemas.catalog import AgentProfileCreate
from agenttape.application.services.approvals import ApprovalService
from agenttape.application.services.profiles import AgentProfileService
from agenttape.application.services.runs import RunService
from agenttape.domain.enums import AdapterAvailability, EventType, FailureCategory, RunStatus
from agenttape.domain.errors import ConflictError
from agenttape.infrastructure.db.models import Event, QueueItem, Run, RunAttempt
from agenttape.worker.executors.agent import AgentRunExecutor
from agenttape.worker.reconcile import reconcile_stale
from agenttape.worker.sink import BatchingEventSink
from agenttape.worker.worker import Worker
from tests.conftest import demo_profile, drive, event_types, start_run


class ScriptAdapter(AgentAdapter):
    """Runs ``python -c <config.code>`` through the shared subprocess helper."""

    name = "test_script"
    display_name = "Test script"

    def discover(self) -> DiscoveryResult:
        return DiscoveryResult(adapter=self.name, status=AdapterAvailability.READY)

    def validate_profile(self, config: dict[str, Any]) -> list[ProfileIssue]:
        return [] if "code" in config else [ProfileIssue("code", "required")]

    async def execute(self, ctx: ExecutionContext) -> AdapterResult:
        env = ctx.build_env(ctx.profile.get("env_allowlist", []))
        if ctx.profile.get("secret"):
            env["SCRIPT_SECRET"] = ctx.resolve_secret(ctx.profile["secret"])
        result = await run_process(
            CommandSpec(argv=[sys.executable, "-c", ctx.profile["code"]], cwd=ctx.workdir, env=env),
            ctx,
        )
        if result.stopped_reason:
            return AdapterResult.failure(
                result.stopped_reason, "stopped", exit_code=result.exit_code
            )
        if result.exit_code == 0:
            return AdapterResult.success(exit_code=0)
        return AdapterResult.failure(
            FailureCategory.ADAPTER_ERROR, f"exit {result.exit_code}", exit_code=result.exit_code
        )


@pytest.fixture
def script(ctx: AppContext) -> Any:
    ctx.adapters.register(ScriptAdapter())

    def make(code: str, name: str = "script", **config: Any) -> str:
        return (
            AgentProfileService(ctx)
            .create(
                AgentProfileCreate(
                    name=name, adapter_type="test_script", config={"code": code, **config}
                )
            )
            .id
        )

    return make


# --------------------------------------------------------------------------- demo scenarios


def test_success_records_events_artifacts_changes_and_evaluation(ctx: AppContext) -> None:
    pid = demo_profile(ctx, evaluators=[{"type": "exit_code", "required": True}])
    detail = drive(ctx, start_run(ctx, pid))
    assert detail.run.status == "succeeded"
    assert detail.run.usage == {
        "input_tokens": detail.run.usage["input_tokens"],
        "output_tokens": detail.run.usage["output_tokens"],
        "simulated": True,
    }
    assert {a.name for a in detail.artifacts} == {"changes.diff", "summary.md"}
    assert {(c.path, c.change_type) for c in detail.file_changes} == {
        ("src/greeter.py", "modified"),
        ("tests/test_greeter.py", "added"),
        ("CHANGELOG.md", "modified"),
    }
    assert [(e.evaluator_type, e.status) for e in detail.evaluations] == [("exit_code", "pass")]
    types = event_types(ctx, detail.run.id)
    for expected in (
        "lifecycle",
        "agent_message",
        "reasoning_summary",
        "tool_call",
        "tool_result",
        "command",
        "stdout",
        "file_write",
        "git_change",
        "artifact",
        "usage",
        "evaluation",
    ):
        assert expected in types
    seqs = [e.sequence for e in RunService(ctx).list_events(detail.run.id, limit=1000).items]
    assert seqs == list(range(1, len(seqs) + 1))
    tool_events = (
        RunService(ctx).list_events(detail.run.id, types=["tool_call", "tool_result"]).items
    )
    calls = {e.id for e in tool_events if e.type == "tool_call"}
    assert all(e.parent_event_id in calls for e in tool_events if e.type == "tool_result")


def test_fail_then_retry_succeeds_on_second_attempt(ctx: AppContext) -> None:
    pid = demo_profile(ctx, "fail_then_retry", max_attempts=2)
    detail = drive(ctx, start_run(ctx, pid))
    assert detail.run.status == "succeeded"
    assert [(a.attempt_number, a.status, a.failure_category) for a in detail.attempts] == [
        (1, "failed", "adapter_error"),
        (2, "succeeded", None),
    ]
    assert "retry" in event_types(ctx, detail.run.id)


def test_retryable_failure_without_attempts_left_fails(ctx: AppContext) -> None:
    pid = demo_profile(ctx, "fail_then_retry", max_attempts=1)
    detail = drive(ctx, start_run(ctx, pid))
    assert detail.run.status == "failed" and detail.run.failure_category == "adapter_error"


def test_fail_scenario_is_not_retried(ctx: AppContext) -> None:
    pid = demo_profile(ctx, "fail", max_attempts=3)
    detail = drive(ctx, start_run(ctx, pid))
    assert detail.run.status == "failed" and detail.run.failure_category == "validation_failed"
    assert len(detail.attempts) == 1


def test_required_evaluation_failure_fails_the_run(ctx: AppContext) -> None:
    pid = demo_profile(
        ctx, evaluators=[{"type": "exit_code", "required": True, "config": {"expected": [3]}}]
    )
    detail = drive(ctx, start_run(ctx, pid))
    assert detail.run.status == "failed" and detail.run.failure_category == "evaluation_failed"
    assert detail.evaluations[0].status == "fail"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _command_pids(ctx: AppContext, run_id: str) -> list[int]:
    events = RunService(ctx).list_events(run_id, types=["command"]).items
    return [int(e.payload["pid"]) for e in events if e.payload.get("pid")]


def test_timeout_kills_the_process_group(ctx: AppContext) -> None:
    pid = demo_profile(ctx, "timeout", timeout_seconds=1.5)
    started = time.monotonic()
    detail = drive(ctx, start_run(ctx, pid))
    assert time.monotonic() - started < 15
    assert detail.run.status == "timed_out" and detail.run.failure_category == "timeout"
    assert detail.attempts[0].status == "timed_out"
    assert "timeout" in event_types(ctx, detail.run.id)
    for p in _command_pids(ctx, detail.run.id):
        assert not _pid_alive(p)


def test_a_timeout_notice_is_written_even_when_teardown_starts_first(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wall-clock watchdog stops the agent, then records its TIMEOUT event. On a busy
    machine that write waits behind the sink's lock and the database thread, so the adapter
    can return and teardown can cancel the watchdogs first; the notice must still be written
    before the attempt is finalized."""
    original = BatchingEventSink.emit

    async def slow_timeout_emit(self: BatchingEventSink, draft: Any) -> None:
        if EventType(draft.type) == EventType.TIMEOUT:
            # Still waiting when the killed adapter returns and teardown begins.
            await asyncio.sleep(2.0)
        await original(self, draft)

    monkeypatch.setattr(BatchingEventSink, "emit", slow_timeout_emit)
    pid = demo_profile(ctx, "timeout", timeout_seconds=1.0)
    detail = drive(ctx, start_run(ctx, pid))
    assert detail.run.status == "timed_out" and detail.run.failure_category == "timeout"
    assert "timeout" in event_types(ctx, detail.run.id)


def test_stopping_the_watchdogs_never_cancels_a_notice_nor_swallows_its_own_cancellation() -> None:
    """When the executor is cancelled (worker shutdown) while teardown waits for a
    notice a watchdog began writing, the notice still completes (it is not cancelled with
    the waiter) and the executor's own cancellation propagates rather than being
    swallowed, so it never goes on to finalize as if it had not been cancelled."""

    async def scenario() -> None:
        gate = asyncio.Event()
        written: list[str] = []

        async def notice() -> None:
            await gate.wait()
            written.append("timeout")

        async def watchdog() -> None:
            await asyncio.Event().wait()

        notices = [asyncio.create_task(notice())]
        stopper = asyncio.create_task(
            AgentRunExecutor._stop([asyncio.create_task(watchdog())], notices)
        )
        for _ in range(3):
            await asyncio.sleep(0)
        stopper.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopper
        gate.set()
        await notices[0]
        assert written == ["timeout"]

    asyncio.run(scenario())


def test_idle_timeout(ctx: AppContext, script: Any) -> None:
    pid = script("import time; print('hi', flush=True); time.sleep(60)", idle_timeout_seconds=1)
    detail = drive(ctx, start_run(ctx, pid))
    assert detail.run.status == "timed_out" and detail.run.failure_category == "idle_timeout"


async def _run_until(ctx: AppContext, run_id: str, predicate: Any, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate(await asyncio.to_thread(RunService(ctx).get_run, run_id)):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


async def test_cancel_running_run_kills_child(ctx: AppContext) -> None:
    pid = demo_profile(ctx, "timeout", timeout_seconds=60)
    run_id = start_run(ctx, pid)
    stop = asyncio.Event()
    worker = Worker(ctx, concurrency=1, poll_interval=0.05)
    task = asyncio.create_task(worker.run(stop))
    try:

        async def has_long_process() -> bool:
            events = await asyncio.to_thread(event_types, ctx, run_id)
            return events.count("command") >= 3

        deadline = time.monotonic() + 20
        while not await has_long_process():
            assert time.monotonic() < deadline
            await asyncio.sleep(0.05)
        out = await asyncio.to_thread(RunService(ctx).cancel_run, run_id, reason="test")
        assert out.status == "cancelling"
        await _run_until(ctx, run_id, lambda r: r.status == "cancelled")
    finally:
        stop.set()
        await task
    detail = RunService(ctx).get_detail(run_id)
    assert detail.attempts[0].status == "cancelled"
    for p in _command_pids(ctx, run_id):
        assert not _pid_alive(p)


def test_needs_approval_pauses_then_resumes_without_redoing_work(ctx: AppContext) -> None:
    pid = demo_profile(ctx, "needs_approval")
    run_id = start_run(ctx, pid)
    paused = drive(ctx, run_id)
    assert paused.run.status == "waiting_for_approval"
    assert paused.attempts[0].status == "waiting_for_approval" and paused.attempts[0].has_checkpoint
    [approval] = ApprovalService(ctx).list_approvals(run_id=run_id).items
    assert approval.status == "pending" and approval.requested_action["action"] == "edit_files"
    with pytest.raises(ConflictError):
        RunService(ctx).retry_run(run_id)
    ApprovalService(ctx).approve(approval.id, reviewer="alice", note="looks fine")
    with pytest.raises(ConflictError):
        ApprovalService(ctx).reject(approval.id, reviewer="bob")
    done = drive(ctx, run_id)
    assert done.run.status == "succeeded"
    assert len(done.attempts) == 1 and done.attempts[0].status == "succeeded"
    types = event_types(ctx, run_id)
    assert types.count("reasoning_summary") == 1  # planning happened once
    assert types.count("approval_requested") == 1 and types.count("approval_granted") == 1
    assert {a.name for a in done.artifacts} >= {"plan.md", "changes.diff", "summary.md"}


def test_rejected_approval_fails_run(ctx: AppContext) -> None:
    run_id = start_run(ctx, demo_profile(ctx, "needs_approval"))
    drive(ctx, run_id)
    [approval] = ApprovalService(ctx).list_approvals(run_id=run_id).items
    out = ApprovalService(ctx).reject(approval.id, reviewer="bob", note="too risky")
    assert out.status == "rejected"
    run = RunService(ctx).get_detail(run_id)
    assert run.run.status == "failed" and run.run.failure_category == "approval_rejected"
    assert "too risky" in (run.run.failure_message or "")
    assert run.attempts[0].status == "failed"


def test_expired_approval_fails_run(ctx: AppContext) -> None:
    run_id = start_run(ctx, demo_profile(ctx, "needs_approval"))
    drive(ctx, run_id)
    assert (
        ApprovalService(ctx).expire_overdue(ctx.clock.now() + timedelta(days=1)) == 0
    )  # no expiry set
    with ctx.session() as session:
        from agenttape.infrastructure.db.models import ApprovalRequest

        session.execute(
            update(ApprovalRequest).values(expires_at=ctx.clock.now() - timedelta(seconds=1))
        )
    assert ApprovalService(ctx).expire_overdue() == 1
    assert RunService(ctx).get_run(run_id).failure_category == "approval_expired"


def test_cancel_while_waiting_for_approval(ctx: AppContext) -> None:
    run_id = start_run(ctx, demo_profile(ctx, "needs_approval"))
    drive(ctx, run_id)
    assert RunService(ctx).cancel_run(run_id).status == "cancelled"
    [approval] = ApprovalService(ctx).list_approvals(run_id=run_id).items
    assert approval.status == "cancelled"
    assert RunService(ctx).get_detail(run_id).attempts[0].status == "cancelled"


# --------------------------------------------------------------------------- process behaviour


def test_heavy_concurrent_output_does_not_deadlock_and_is_bounded(
    ctx: AppContext, script: Any
) -> None:
    code = (
        "import sys\n"
        "for i in range(40000):\n"
        "    sys.stdout.write(f'out line {i} ' + 'x' * 60 + '\\n')\n"
        "    sys.stderr.write(f'err line {i}\\n')\n"
        "sys.stdout.write('y' * 200000)\n"  # one enormous line with no newline
    )
    detail = drive(ctx, start_run(ctx, script(code)), timeout=60)
    assert detail.run.status == "succeeded"
    attempt = detail.attempts[0]
    log = ctx.settings.logs_dir / detail.run.id / "attempt-1.stdout.log"
    assert log.stat().st_size > 2_800_000
    assert "out line 39999" in log.read_text()
    with ctx.session() as session:
        stored = session.scalars(
            select(Event).where(Event.run_id == detail.run.id, Event.type == "stdout")
        ).all()
    assert sum(len(e.payload.get("text", "")) for e in stored) <= 2_200_000
    warnings = RunService(ctx).list_events(detail.run.id, types=["warning"]).items
    assert {w.payload["stream"] for w in warnings} == {"stdout"}  # stored-event budget notice
    assert len(stored) < 1000  # interleaved streams still batch
    assert attempt.stdout_bytes == log.stat().st_size and attempt.stderr_bytes > 0


def test_secrets_are_redacted_from_events_and_logs(
    ctx: AppContext, script: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_value = "s3cr3t-" + "value-" + "8472"
    monkeypatch.setenv("MY_SERVICE_TOKEN", secret_value)
    code = "import os; print('token is', os.environ['SCRIPT_SECRET']); print('aws AKIA' + 'ABCDEFGHIJKLMNOP')"
    detail = drive(ctx, start_run(ctx, script(code, secret="MY_SERVICE_TOKEN")))
    assert detail.run.status == "succeeded"
    with ctx.session() as session:
        rows = session.scalars(select(Event).where(Event.run_id == detail.run.id)).all()
    blob = " ".join(str(e.payload) + e.summary for e in rows)
    fake_key = "AKIA" + "ABCDEFGHIJKLMNOP"
    assert secret_value not in blob and fake_key not in blob
    assert "[REDACTED:secret]" in blob and "[REDACTED:aws_access_key]" in blob
    log = (ctx.settings.logs_dir / detail.run.id / "attempt-1.stdout.log").read_text()
    assert secret_value not in log and "[REDACTED:secret]" in log


def test_missing_secret_is_a_config_error(ctx: AppContext, script: Any) -> None:
    detail = drive(ctx, start_run(ctx, script("print(1)", secret="DEFINITELY_NOT_SET_ANYWHERE")))
    assert detail.run.status == "failed" and detail.run.failure_category == "config_error"


def test_env_is_allowlisted(ctx: AppContext, script: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEAKY_VAR", "should-not-pass")
    monkeypatch.setenv("ALLOWED_VAR", "passes")
    code = "import os; print('LEAKY', os.environ.get('LEAKY_VAR')); print('ALLOWED', os.environ.get('ALLOWED_VAR'))"
    detail = drive(ctx, start_run(ctx, script(code, env_allowlist=["ALLOWED_VAR"])))
    text = " ".join(
        str(e.payload) for e in RunService(ctx).list_events(detail.run.id, types=["stdout"]).items
    )
    assert "LEAKY None" in text and "ALLOWED passes" in text


def test_missing_executable_fails_cleanly(ctx: AppContext) -> None:
    class Missing(ScriptAdapter):
        name = "test_missing"

        async def execute(self, ctx: ExecutionContext) -> AdapterResult:
            result = await run_process(CommandSpec(argv=["/nonexistent/agent-cli"]), ctx)
            assert result.spawn_error
            return AdapterResult.failure(FailureCategory.ADAPTER_UNAVAILABLE, result.spawn_error)

    ctx.adapters.register(Missing())
    pid = (
        AgentProfileService(ctx)
        .create(AgentProfileCreate(name="m", adapter_type="test_missing", config={"code": ""}))
        .id
    )
    detail = drive(ctx, start_run(ctx, pid))
    assert detail.run.failure_category == "adapter_unavailable"
    assert "error" in event_types(ctx, detail.run.id)


# --------------------------------------------------------------------------- reconciliation


def _make_stale(ctx: AppContext, run_id: str, status: str = "running") -> None:
    now = ctx.clock.now()
    with ctx.session() as session:
        session.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(status=status, started_at=now, attempt_count=1)
        )
        session.add(
            RunAttempt(
                run_id=run_id,
                attempt_number=1,
                status=status,
                worker_id="dead-worker",
                pid=999999,
                created_at=now,
                started_at=now,
                heartbeat_at=now - timedelta(minutes=5),
                lease_expires_at=now - timedelta(minutes=4),
            )
        )
        session.execute(
            update(QueueItem)
            .where(QueueItem.run_id == run_id)
            .values(
                status="leased",
                lease_owner="dead-worker",
                lease_expires_at=now - timedelta(minutes=4),
            )
        )


def test_stale_attempt_is_interrupted_and_failed(ctx: AppContext) -> None:
    run_id = start_run(ctx, demo_profile(ctx))
    _make_stale(ctx, run_id)
    report = reconcile_stale(ctx)
    assert report["interrupted"] == [run_id]
    detail = RunService(ctx).get_detail(run_id)
    assert detail.run.status == "failed" and detail.run.failure_category == "interrupted"
    assert detail.attempts[0].failure_category == "interrupted"
    assert reconcile_stale(ctx)["interrupted"] == []  # idempotent
    with ctx.session() as session:
        assert (
            session.scalars(select(QueueItem.status).where(QueueItem.run_id == run_id)).one()
            == "done"
        )


def test_stale_attempt_is_requeued_when_retries_remain(ctx: AppContext) -> None:
    run_id = start_run(ctx, demo_profile(ctx, max_attempts=2))
    _make_stale(ctx, run_id)
    assert reconcile_stale(ctx)["requeued"] == [run_id]
    detail = drive(ctx, run_id)
    assert detail.run.status == "succeeded"
    assert [a.status for a in detail.attempts] == ["failed", "succeeded"]


def test_stale_cancelling_attempt_becomes_cancelled(ctx: AppContext) -> None:
    run_id = start_run(ctx, demo_profile(ctx, max_attempts=3))
    _make_stale(ctx, run_id, status="cancelling")
    with ctx.session() as session:
        session.execute(
            update(Run).where(Run.id == run_id).values(cancel_requested_at=ctx.clock.now())
        )
    assert reconcile_stale(ctx)["cancelled"] == [run_id]
    assert RunService(ctx).get_run(run_id).status == RunStatus.CANCELLED


def test_claimed_but_never_started_item_is_reopened(ctx: AppContext) -> None:
    run_id = start_run(ctx, demo_profile(ctx))
    claim = ctx.queue.claim("doomed-worker", 0.001)
    assert claim is not None and claim.run_id == run_id
    time.sleep(0.01)
    assert reconcile_stale(ctx)["queue_reopened"] == 1
    assert drive(ctx, run_id).run.status == "succeeded"


def test_worker_heartbeat_is_recorded(ctx: AppContext) -> None:
    from agenttape.infrastructure.db.models import WorkerHeartbeat

    async def go() -> None:
        stop = asyncio.Event()
        worker = Worker(ctx, worker_id="hb-worker", poll_interval=0.05)
        task = asyncio.create_task(worker.run(stop))
        await asyncio.sleep(0.3)
        stop.set()
        await task

    asyncio.run(go())
    with ctx.session() as session:
        hb = session.get(WorkerHeartbeat, "hb-worker")
        assert hb is not None and hb.status == "stopped" and hb.stopped_at is not None
