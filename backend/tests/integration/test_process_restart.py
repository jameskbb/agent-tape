"""Acceptance criterion 6 with real OS processes: a worker process is killed (SIGKILL, no
clean shutdown) while a workflow waits for approval; a fresh worker process with a new
identity resumes it after approval without rerunning completed nodes."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import select

from agenttape.application.services.approvals import ApprovalService
from agenttape.application.services.runs import RunService
from agenttape.bootstrap import build_context
from agenttape.config import Settings
from agenttape.domain.enums import RunStatus
from agenttape.infrastructure.db.models import QueueItem
from tests.conftest import demo_profile
from tests.integration.test_workflows import (
    children,
    create_workflow,
    pending_approval,
    safe_change_definition,
    start,
    started_events,
    steps,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX signals")


@contextmanager
def worker_process(settings: Settings) -> Iterator[subprocess.Popen[bytes]]:
    env = {
        **os.environ,
        "AGENTTAPE_DATA_DIR": str(settings.data_dir),
        "AGENTTAPE_ALLOWED_ROOTS": os.pathsep.join(str(r) for r in settings.allowed_roots),
        "AGENTTAPE_DEMO_STEP_DELAY_MS": "0",
        "AGENTTAPE_WORKER_POLL_INTERVAL_SECONDS": "0.1",
        "AGENTTAPE_HEARTBEAT_INTERVAL_SECONDS": "0.5",
    }
    if settings.database_url:  # PostgreSQL suite: the worker must use the test's database
        env["DATABASE_URL"] = settings.database_url
    log = (settings.data_dir / f"worker-{time.monotonic_ns()}.log").open("wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "agenttape.cli.main", "worker", "--concurrency", "2"],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=30)
        log.close()


def wait_for(predicate: Callable[[], bool], timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


def test_workflow_resumes_in_a_new_worker_process_after_a_hard_kill(settings: Settings) -> None:
    ctx = build_context(settings, migrate=True)
    try:
        workflow_id = create_workflow(ctx, safe_change_definition(demo_profile(ctx)))
        run_id = start(ctx, workflow_id, goal="the greeter")
        service = RunService(ctx)

        def status() -> RunStatus:
            return RunStatus(service.get_run(run_id).status)

        with worker_process(settings) as first:
            wait_for(lambda: status() == RunStatus.WAITING_FOR_APPROVAL, 90, "the approval pause")
            first.send_signal(signal.SIGKILL)  # a crash: no shutdown hooks run
            first.wait(timeout=30)
        paused = {k: s.status for k, s in steps(ctx, run_id).items()}
        assert paused["approve"] == "waiting_for_approval"
        assert paused["prep"] == paused["agent_a"] == paused["agent_b"] == "succeeded"
        assert len(children(ctx, run_id)) == 2

        with ctx.session() as session:
            items_before = set(
                session.scalars(select(QueueItem.id).where(QueueItem.run_id == run_id))
            )
        ApprovalService(ctx).approve(pending_approval(ctx, run_id), reviewer="reviewer")

        with worker_process(settings) as second:
            wait_for(lambda: status() == RunStatus.SUCCEEDED, 90, "the resumed run to succeed")
            second_pid = second.pid

        after = steps(ctx, run_id)
        assert all(s.status == "succeeded" for s in after.values())
        assert {k: s.attempt_count for k, s in after.items()} == dict.fromkeys(after, 1)
        assert started_events(ctx, run_id) == dict.fromkeys(after, 1)
        assert len(children(ctx, run_id)) == 2
        with ctx.session() as session:
            resumed = session.scalars(
                select(QueueItem).where(
                    QueueItem.run_id == run_id, QueueItem.id.not_in(items_before)
                )
            ).all()
        assert resumed
        # Worker ids are "<host>:<pid>:<random>": the resume ran in the second process.
        assert all(f":{second_pid}:" in (i.lease_owner or "") for i in resumed)
    finally:
        ctx.close()
