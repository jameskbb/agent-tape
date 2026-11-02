"""Herdr runtime adapter against the fake Herdr server (M2).

Covers workspace/agent creation, prompt/wait/blocked/done/idle/unknown/timeout, event
subscription reconnect, AgentTape restart + reattach without duplicate events, stale and
replaced ids, ownership-safe cleanup, unavailable Herdr with and without fallback, remote
machine labels, and the runtime API. The demo kind runs the real evidence demo agent
(``agenttape.demo_agent``) in the AgentTape fixture repository, and one test proves a
Herdr run records the same file and context evidence as a local one. No real Herdr, SSH
host, or agent CLI is used.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from agenttape.adapters.builtin._demo_repo import ensure_fixture_repo
from agenttape.application.context import AppContext
from agenttape.application.schemas.catalog import AgentProfileCreate, ProjectCreate, ProjectSettings
from agenttape.application.schemas.runs import RunCreate
from agenttape.application.services.approvals import ApprovalService
from agenttape.application.services.profiles import AgentProfileService
from agenttape.application.services.projects import ProjectService
from agenttape.application.services.runs import RunService
from agenttape.domain.enums import EventType, RunStatus
from agenttape.infrastructure.db.extra_models.context_evidence import ContextEvidenceEvent
from agenttape.infrastructure.db.extra_models.file_evidence import (
    FileEvidenceEvent,
    RunFileSummary,
)
from agenttape.infrastructure.db.extra_models.runtime_sessions import RuntimeSession
from agenttape.infrastructure.db.models import QueueItem, RunAttempt
from agenttape.worker.reconcile import reconcile_stale
from agenttape.worker.worker import Worker
from tests.conftest import demo_profile, drive, start_run
from tests.fixtures.fake_herdr import FakeHerdr

#: Demo agent pacing for tests that act while it is still running (it has ~40 steps).
SLOW_MS = 300


@pytest.fixture
def fake_herdr(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeHerdr]:
    # Unix socket paths are limited to ~100 bytes, so use a short directory.
    tmp = Path(tempfile.mkdtemp(prefix="fh", dir="/tmp"))
    sock = tmp / "h.sock"
    fake = FakeHerdr(work_seconds=0.3).start(sock)
    monkeypatch.setenv("AGENTTAPE_HERDR_SOCKET", str(sock))
    yield fake
    fake.stop()
    shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- helpers


@pytest.fixture
def evidence_project(ctx: AppContext) -> str:
    """The evidence demo project: the AgentTape fixture repository with Git isolation and
    the seed's protected paths (as ``seeds/file_evidence_demo.py`` creates it)."""
    repo = ensure_fixture_repo(ctx.settings.data_dir)
    settings = ProjectSettings(
        git_isolation=True, protected_paths=[".github/**", "*.lock", "CHANGELOG.md"]
    )
    create = ProjectCreate(name="Evidence demo", repo_path=str(repo), settings=settings)
    return ProjectService(ctx).create(create).id


def herdr_profile(
    ctx: AppContext, name: str = "herdr", *, project_id: str | None = None, **config: Any
) -> str:
    cfg = {"agent_kind": "demo", **config}
    create = AgentProfileCreate(name=name, adapter_type="herdr", project_id=project_id, config=cfg)
    return AgentProfileService(ctx).create(create).id


def start_demo(ctx: AppContext, project_id: str, **config: Any) -> str:
    """A Herdr demo run in the evidence project."""
    return start_run(
        ctx, herdr_profile(ctx, project_id=project_id, **config), project_id=project_id
    )


def telemetry_reads(ctx: AppContext, run_id: str) -> int:
    """File read/search records the demo agent wrote to its Herdr telemetry file."""
    [path] = (ctx.settings.logs_dir / run_id).glob("*.herdr-telemetry.jsonl")
    kinds = [json.loads(line).get("type") for line in path.read_text().splitlines()]
    return sum(k in ("file_read", "file_search") for k in kinds)


def file_reads(ctx: AppContext, run_id: str) -> int:
    return len([e for e in events(ctx, run_id) if e.type == EventType.FILE_READ])


def create_run(ctx: AppContext, profile_id: str, prompt: str = "Do the thing.") -> str:
    return RunService(ctx).create_run(RunCreate(agent_profile_id=profile_id, prompt=prompt)).run.id


def maybe_row(ctx: AppContext, run_id: str) -> RuntimeSession | None:
    with ctx.session() as session:
        row = session.scalars(select(RuntimeSession).where(RuntimeSession.run_id == run_id)).first()
        if row is not None:
            session.expunge(row)
        return row


def runtime_row(ctx: AppContext, run_id: str) -> RuntimeSession:
    row = maybe_row(ctx, run_id)
    assert row is not None
    return row


def events(ctx: AppContext, run_id: str) -> list[Any]:
    return list(RunService(ctx).list_events(run_id, limit=1000).items)


def runtime_actions(ctx: AppContext, run_id: str) -> list[str]:
    return [e.payload.get("action") for e in events(ctx, run_id) if e.type == EventType.RUNTIME]


def approve(ctx: AppContext, run_id: str) -> None:
    [approval] = [
        a for a in ApprovalService(ctx).list_approvals(run_id=run_id).items if a.status == "pending"
    ]
    ApprovalService(ctx).approve(approval.id, reviewer="tester")


def wait_until(predicate: Callable[[], bool], timeout: float = 20.0, what: str = "it") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def row_is(ctx: AppContext, run_id: str, **expected: Any) -> Callable[[], bool]:
    def check() -> bool:
        row = maybe_row(ctx, run_id)
        return row is not None and all(getattr(row, k) == v for k, v in expected.items())

    return check


def has_telemetry(ctx: AppContext, run_id: str) -> Callable[[], bool]:
    def check() -> bool:
        row = maybe_row(ctx, run_id)
        return row is not None and row.telemetry_offset > 0

    return check


def status_is(ctx: AppContext, run_id: str, status: RunStatus) -> Callable[[], bool]:
    return lambda: RunService(ctx).get_run(run_id).status == status


def fake_calls(fake: FakeHerdr, method: str) -> list[dict[str, Any]]:
    return [params for m, params in list(fake.calls) if m == method]


class BackgroundWorker:
    """A worker in its own thread and event loop; ``shutdown`` stops it like SIGTERM."""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stop: asyncio.Event | None = None
        self.thread = threading.Thread(target=self._main, daemon=True)
        self.ready = threading.Event()

    def _main(self) -> None:
        self.loop = asyncio.new_event_loop()

        async def run() -> None:
            self.stop = asyncio.Event()
            self.ready.set()
            await Worker(self.ctx, worker_id="bg-worker", concurrency=1).run(self.stop)

        self.loop.run_until_complete(run())
        self.loop.close()

    def start(self) -> BackgroundWorker:
        self.thread.start()
        assert self.ready.wait(5)
        return self

    def shutdown(self) -> None:
        assert self.loop is not None and self.stop is not None
        self.loop.call_soon_threadsafe(self.stop.set)
        self.thread.join(30)
        assert not self.thread.is_alive()


# --------------------------------------------------------------------------- demo kind


def test_demo_run_pauses_for_approval_and_resumes_in_the_same_pane(
    ctx: AppContext, fake_herdr: FakeHerdr, evidence_project: str
) -> None:
    run_id = start_demo(ctx, evidence_project)
    detail = drive(ctx, run_id, timeout=60)
    assert detail.run.status == RunStatus.WAITING_FOR_APPROVAL, detail.run.failure_message
    row = runtime_row(ctx, run_id)
    assert row.runtime == "herdr" and row.status == "waiting"
    assert row.owned_workspace and row.pane_id and row.workspace_id and row.tab_id
    assert row.herdr_version == "0.9.0-fake" and row.protocol == 22
    assert row.lifecycle_state == "blocked" and row.telemetry_offset > 0
    assert fake_herdr.panes[row.pane_id].status == "blocked"  # visible in Herdr
    [approval] = detail.approvals
    assert approval.type == "protected_path"  # the same approval as the local demo
    reads = file_reads(ctx, run_id)
    assert reads == telemetry_reads(ctx, run_id) > 0
    # The agent ran in the run's isolated worktree of the fixture, not the scratch dir.
    [typed] = fake_calls(fake_herdr, "pane.send_text")
    assert f"--workdir {detail.attempts[0].workdir} " in typed["text"]
    assert "--variant a --step-delay-ms 0" in typed["text"]

    approve(ctx, run_id)
    detail = drive(ctx, run_id, timeout=60)
    assert detail.run.status == RunStatus.SUCCEEDED, detail.run.failure_message
    row2 = runtime_row(ctx, run_id)
    assert row2.pane_id == row.pane_id  # same pane, same attempt row
    assert row2.status == "completed" and row2.lifecycle_state == "idle"
    writes = [e.payload["path"] for e in events(ctx, run_id) if e.type == EventType.FILE_WRITE]
    assert ".github/workflows/ci.yml" in writes
    assert file_reads(ctx, run_id) == reads
    actions = runtime_actions(ctx, run_id)
    assert actions.count("start") >= 3  # workspace, demo start, demo resume
    assert "read" not in actions  # the demo's output is its telemetry, not the pane
    assert "herdr-recent-output.txt" not in [a.name for a in detail.artifacts]
    # Keep-by-default cleanup: the AgentTape-owned workspace is still there.
    assert row.workspace_id in fake_herdr.workspaces
    payload_text = str([e.payload for e in events(ctx, run_id) if e.type == EventType.RUNTIME])
    assert "h.sock" not in payload_text  # no socket paths in events


def run_through_approval(ctx: AppContext, run_id: str) -> None:
    assert drive(ctx, run_id, timeout=90).run.status == RunStatus.WAITING_FOR_APPROVAL
    approve(ctx, run_id)
    detail = drive(ctx, run_id, timeout=90)
    assert detail.run.status == RunStatus.SUCCEEDED, detail.run.failure_message


def file_ledger(ctx: AppContext, run_id: str) -> dict[str, str]:
    with ctx.session() as session:
        rows = session.scalars(select(RunFileSummary).where(RunFileSummary.run_id == run_id))
        return {r.file_key: r.classification for r in rows}


def context_trail(ctx: AppContext, run_id: str) -> set[tuple[str | None, str]]:
    with ctx.session() as session:
        rows = session.scalars(
            select(ContextEvidenceEvent).where(ContextEvidenceEvent.run_id == run_id)
        )
        # The agent profile is a context source keyed by its id; the two runs use two profiles.
        return {
            ("profile" if (r.source_key or "").startswith("profile:") else r.source_key, r.stage)
            for r in rows
        }


def evidence_runtimes(ctx: AppContext, run_id: str) -> set[str | None]:
    with ctx.session() as session:
        return set(
            session.scalars(
                select(FileEvidenceEvent.runtime).where(FileEvidenceEvent.run_id == run_id)
            )
        )


@pytest.mark.slow
def test_a_herdr_demo_run_records_the_same_evidence_as_a_local_one(
    ctx: AppContext, fake_herdr: FakeHerdr, evidence_project: str
) -> None:
    """A run executed in Herdr produces the same file and context evidence."""
    pacing = {"step_delay_ms": 60, "variant": "b"}
    local_profile = demo_profile(ctx, "full_evidence", name="local evidence", **pacing)
    local = start_run(ctx, local_profile, project_id=evidence_project)
    herdr = start_demo(ctx, evidence_project, **pacing)
    run_through_approval(ctx, local)
    run_through_approval(ctx, herdr)

    ledger = file_ledger(ctx, local)
    assert ledger["README.md"] == "restored" and ".github/workflows/ci.yml" in ledger
    assert file_ledger(ctx, herdr) == ledger
    trail = context_trail(ctx, local)
    assert {stage for _key, stage in trail} >= {"retrieved", "cited", "unavailable"}
    assert context_trail(ctx, herdr) == trail
    assert evidence_runtimes(ctx, local) == {"local"}
    assert evidence_runtimes(ctx, herdr) == {"herdr"}
    # Its scripted findings arrive too, which is why a Herdr demo run's findings capture is
    # "scripted" (HerdrAdapter.findings_capture_for), not "unsupported".
    local_findings = [
        e.payload.get("id") for e in events(ctx, local) if e.type == EventType.FINDING
    ]
    herdr_findings = [
        e.payload.get("id") for e in events(ctx, herdr) if e.type == EventType.FINDING
    ]
    assert local_findings and herdr_findings == local_findings


# --------------------------------------------------------------------------- claude / codex


def test_claude_agent_is_started_prompted_and_observed_to_done(
    ctx: AppContext, fake_herdr: FakeHerdr
) -> None:
    run_id = create_run(ctx, herdr_profile(ctx, agent_kind="claude"), prompt="Refactor greeter")
    detail = drive(ctx, run_id)
    assert detail.run.status == RunStatus.SUCCEEDED, detail.run.failure_message
    row = runtime_row(ctx, run_id)
    assert row.agent_name and row.agent_name.startswith("at-") and row.agent_kind == "claude"
    assert row.owned_agent and row.owned_workspace and row.status == "completed"
    assert row.lifecycle_state == "done"
    assert row.native_session_kind == "id"  # presence only
    [start] = fake_calls(fake_herdr, "agent.start")
    assert start["kind"] == "claude" and start["pane_id"] == row.pane_id
    [prompt] = fake_calls(fake_herdr, "agent.prompt")
    assert prompt["text"] == "Refactor greeter" and prompt["target"] == row.agent_name
    assert not fake_calls(fake_herdr, "pane.send_text")  # the prompt is never raw keystrokes
    states = [
        e.payload.get("state")
        for e in events(ctx, run_id)
        if e.type == EventType.RUNTIME and e.payload.get("action") == "wait"
    ]
    assert "working" in states and states[-1] == "done"
    everything = str([(e.summary, e.payload) for e in events(ctx, run_id)]) + str(row.details)
    assert "fake-session-secret-id" not in everything  # the native session value is not kept
    runtime_payloads = [e.payload for e in events(ctx, run_id) if e.type == EventType.RUNTIME]
    assert "Refactor greeter" not in str(runtime_payloads)
    assert "herdr-recent-output.txt" in [a.name for a in detail.artifacts]


def test_blocked_agent_is_left_untouched_until_resolved_in_herdr(
    ctx: AppContext, fake_herdr: FakeHerdr
) -> None:
    profile = herdr_profile(ctx, agent_kind="codex", idle_timeout_seconds=1)
    run_id = create_run(ctx, profile, prompt="Please BLOCK on a question")
    worker = BackgroundWorker(ctx).start()
    try:
        wait_until(row_is(ctx, run_id, lifecycle_state="blocked"), what="blocked")
        time.sleep(2.5)  # longer than the idle timeout: waiting on a person is not idleness
        assert RunService(ctx).get_run(run_id).status == RunStatus.RUNNING
        assert not fake_calls(fake_herdr, "agent.send_keys")
        blocked = [e for e in events(ctx, run_id) if e.payload.get("action") == "blocked"]
        assert blocked and "herdr agent attach" in (blocked[0].payload["attach_command"] or "")
        fake_herdr.set_status(runtime_row(ctx, run_id).pane_id or "", "done")  # a person answered
        wait_until(status_is(ctx, run_id, RunStatus.SUCCEEDED), what="success")
    finally:
        worker.shutdown()


def test_unknown_state_is_not_treated_as_completion(ctx: AppContext, fake_herdr: FakeHerdr) -> None:
    profile = herdr_profile(ctx, agent_kind="claude", timeout_seconds=3)
    run_id = create_run(ctx, profile, prompt="End UNKNOWN")
    detail = drive(ctx, run_id)
    assert detail.run.status == RunStatus.TIMED_OUT
    assert runtime_row(ctx, run_id).lifecycle_state == "unknown"
    [keys] = fake_calls(fake_herdr, "agent.send_keys")
    assert keys["keys"] == ["ctrl+c"]
    assert "interrupt" in runtime_actions(ctx, run_id)


def test_cancel_interrupts_a_working_agent(ctx: AppContext, fake_herdr: FakeHerdr) -> None:
    run_id = create_run(ctx, herdr_profile(ctx, agent_kind="claude"), prompt="HANG forever")
    worker = BackgroundWorker(ctx).start()
    try:
        wait_until(row_is(ctx, run_id, lifecycle_state="working"), what="working")
        RunService(ctx).cancel_run(run_id, reason="changed my mind")
        wait_until(status_is(ctx, run_id, RunStatus.CANCELLED), what="cancel")
    finally:
        worker.shutdown()
    assert fake_calls(fake_herdr, "agent.send_keys") == [
        {"target": runtime_row(ctx, run_id).agent_name, "keys": ["ctrl+c"]}
    ]


def test_event_subscription_reconnects_after_the_stream_drops(
    ctx: AppContext, fake_herdr: FakeHerdr
) -> None:
    fake_herdr.work_seconds = 3.0
    run_id = create_run(ctx, herdr_profile(ctx, agent_kind="claude"), prompt="work a while")
    worker = BackgroundWorker(ctx).start()
    try:
        wait_until(row_is(ctx, run_id, lifecycle_state="working"), what="working")
        time.sleep(0.3)
        fake_herdr.drop_subscriptions()
        wait_until(status_is(ctx, run_id, RunStatus.SUCCEEDED), timeout=30, what="success")
    finally:
        worker.shutdown()
    summaries = [e.summary for e in events(ctx, run_id) if e.payload.get("action") == "resubscribe"]
    assert any("lost" in s for s in summaries) and any("restored" in s for s in summaries)


# --------------------------------------------------------------------------- availability


def test_unavailable_herdr_fails_clearly_without_fallback(ctx: AppContext) -> None:
    run_id = start_run(ctx, herdr_profile(ctx))
    detail = drive(ctx, run_id)
    assert detail.run.status == RunStatus.FAILED
    assert detail.run.failure_category == "adapter_unavailable"
    row = runtime_row(ctx, run_id)
    assert row.status == "failed" and row.failure_category == "unavailable"
    assert "unavailable" in runtime_actions(ctx, run_id)


def test_unavailable_herdr_falls_back_to_local_before_start_when_configured(
    ctx: AppContext,
) -> None:
    profile = herdr_profile(ctx, fallback="local", local_config={"scenario": "success"})
    run_id = start_run(ctx, profile)
    detail = drive(ctx, run_id)
    assert detail.run.status == RunStatus.SUCCEEDED, detail.run.failure_message
    row = runtime_row(ctx, run_id)
    assert row.runtime == "local" and row.status == "fallback"
    [fallback] = [e for e in events(ctx, run_id) if e.payload.get("action") == "fallback"]
    assert fallback.payload["fallback_to"] == "demo" and fallback.source == "herdr"
    # After the fallback the attempt is labelled as the local run it was.
    later = [e for e in events(ctx, run_id) if e.sequence > fallback.sequence]
    assert later and "herdr" not in {e.source for e in later}
    assert "herdr" not in evidence_runtimes(ctx, run_id)


def test_no_fallback_after_the_attempt_started_in_herdr(
    ctx: AppContext, fake_herdr: FakeHerdr, monkeypatch: pytest.MonkeyPatch, evidence_project: str
) -> None:
    from agenttape.adapters.builtin import herdr as adapter_module

    monkeypatch.setattr(adapter_module, "UNREACHABLE_GRACE_SECONDS", 0.5)
    run_id = start_demo(
        ctx, evidence_project, step_delay_ms=SLOW_MS, fallback="local", local_config={}
    )
    worker = BackgroundWorker(ctx).start()
    try:
        wait_until(has_telemetry(ctx, run_id), what="telemetry")
        fake_herdr.stop()  # Herdr goes away mid-run
        wait_until(status_is(ctx, run_id, RunStatus.FAILED), timeout=30, what="failure")
    finally:
        worker.shutdown()
    row = runtime_row(ctx, run_id)
    assert row.runtime == "herdr" and row.failure_category == "unavailable"
    assert "fallback" not in runtime_actions(ctx, run_id)


# --------------------------------------------------------------------------- stale / replaced


def test_closed_pane_is_a_stale_id_failure(
    ctx: AppContext, fake_herdr: FakeHerdr, evidence_project: str
) -> None:
    run_id = start_demo(ctx, evidence_project, step_delay_ms=SLOW_MS)
    worker = BackgroundWorker(ctx).start()
    try:
        wait_until(has_telemetry(ctx, run_id), what="telemetry")
        fake_herdr.close_pane(runtime_row(ctx, run_id).pane_id or "")
        wait_until(status_is(ctx, run_id, RunStatus.FAILED), what="failure")
    finally:
        worker.shutdown()
    assert runtime_row(ctx, run_id).failure_category == "stale_id"


def test_a_restart_mid_prompt_never_sends_the_prompt_twice(
    ctx: AppContext, fake_herdr: FakeHerdr
) -> None:
    run_id = create_run(ctx, herdr_profile(ctx, agent_kind="claude"), prompt="HANG a while")
    worker = BackgroundWorker(ctx).start()
    wait_until(row_is(ctx, run_id, lifecycle_state="working"), what="working")
    worker.shutdown()
    # As if AgentTape died after agent.prompt but before recording that it was sent.
    with ctx.session() as session:
        row = session.scalars(select(RuntimeSession).where(RuntimeSession.run_id == run_id)).one()
        row.details = {**row.details, "prompted": False, "prompting": True}
    fake_herdr.set_status(runtime_row(ctx, run_id).pane_id or "", "done")
    assert drive(ctx, run_id).run.status == RunStatus.SUCCEEDED
    assert len(fake_calls(fake_herdr, "agent.prompt")) == 1


def test_replaced_agent_is_detected(ctx: AppContext, fake_herdr: FakeHerdr) -> None:
    run_id = create_run(ctx, herdr_profile(ctx, agent_kind="claude"), prompt="HANG please")
    worker = BackgroundWorker(ctx).start()
    try:
        wait_until(row_is(ctx, run_id, lifecycle_state="working"), what="working")
        fake_herdr.replace_agent(runtime_row(ctx, run_id).pane_id or "")
        wait_until(status_is(ctx, run_id, RunStatus.FAILED), timeout=30, what="failure")
    finally:
        worker.shutdown()
    assert runtime_row(ctx, run_id).failure_category == "agent_replaced"


# --------------------------------------------------------------------------- cleanup ownership


def test_cleanup_closes_only_what_agenttape_created(ctx: AppContext, fake_herdr: FakeHerdr) -> None:
    # 1. An AgentTape-owned workspace is closed when cleanup=close_owned.
    profile = herdr_profile(ctx, "owned", agent_kind="claude", cleanup="close_owned")
    run_id = create_run(ctx, profile)
    assert drive(ctx, run_id).run.status == RunStatus.SUCCEEDED
    assert runtime_row(ctx, run_id).workspace_id not in fake_herdr.workspaces
    assert "cleanup" in runtime_actions(ctx, run_id)

    # 2. A person's workspace is reused: only AgentTape's tab is closed, never the workspace.
    created = fake_herdr.run(fake_herdr._dispatch, "workspace.create", {"label": "mine"})
    ws_id = created["workspace"]["workspace_id"]
    profile = herdr_profile(
        ctx,
        "reuse-ws",
        agent_kind="claude",
        cleanup="close_owned",
        workspace_policy="reuse",
        reuse_workspace_id=ws_id,
    )
    run_id = create_run(ctx, profile)
    assert drive(ctx, run_id).run.status == RunStatus.SUCCEEDED
    row = runtime_row(ctx, run_id)
    assert row.workspace_id == ws_id and not row.owned_workspace
    assert ws_id in fake_herdr.workspaces
    assert row.tab_id not in fake_herdr.tabs
    assert f"{ws_id}:p1" in fake_herdr.panes  # the person's own pane is untouched

    # 3. A reused agent is prompted but never started or closed.
    other = create_run(ctx, herdr_profile(ctx, "starter", agent_kind="claude"))
    assert drive(ctx, other).run.status == RunStatus.SUCCEEDED
    existing = runtime_row(ctx, other).agent_name
    starts = len(fake_calls(fake_herdr, "agent.start"))
    profile = herdr_profile(
        ctx,
        "reuse-agent",
        agent_kind="claude",
        cleanup="close_owned",
        workspace_policy="reuse",
        reuse_agent=existing,
    )
    run_id = create_run(ctx, profile)
    assert drive(ctx, run_id).run.status == RunStatus.SUCCEEDED
    row = runtime_row(ctx, run_id)
    assert row.agent_name == existing and not row.owned_agent and not row.owned_workspace
    assert len(fake_calls(fake_herdr, "agent.start")) == starts
    assert row.workspace_id in fake_herdr.workspaces


# --------------------------------------------------------------------------- restart / reattach


def test_restart_reattaches_and_continues_without_duplicate_events(
    ctx: AppContext, fake_herdr: FakeHerdr, evidence_project: str
) -> None:
    run_id = start_demo(ctx, evidence_project, step_delay_ms=SLOW_MS)
    worker = BackgroundWorker(ctx).start()
    wait_until(has_telemetry(ctx, run_id), what="telemetry")
    worker.shutdown()  # AgentTape stops mid-run; the agent keeps running in Herdr

    row = runtime_row(ctx, run_id)
    assert row.status == "detached"
    assert RunService(ctx).get_run(run_id).status == RunStatus.QUEUED
    with ctx.session() as session:
        attempt = session.scalars(select(RunAttempt).where(RunAttempt.run_id == run_id)).one()
        assert "herdr_reattach" in (attempt.checkpoint or {})
        items = session.scalars(select(QueueItem).where(QueueItem.run_id == run_id)).all()
        assert [i.status for i in items].count("queued") == 1  # one item for the reattach
        assert "leased" not in [i.status for i in items]

    detail = drive(ctx, run_id, timeout=60)  # a new AgentTape process picks it up
    assert detail.run.status == RunStatus.WAITING_FOR_APPROVAL, detail.run.failure_message
    assert detail.run.attempt_count == 1  # same attempt, same pane
    assert runtime_row(ctx, run_id).pane_id == row.pane_id
    evs = events(ctx, run_id)
    reads = file_reads(ctx, run_id)
    assert reads == telemetry_reads(ctx, run_id)  # every record once: no duplicates
    [reconnect] = [e for e in evs if e.payload.get("action") == "reconnect"]
    assert reconnect.payload["runtime"] == "herdr"
    assert reconnect.payload["offline_from"] and reconnect.payload["offline_to"]
    assert any(e.summary.startswith("Reattaching attempt 1") for e in evs)

    approve(ctx, run_id)
    assert drive(ctx, run_id, timeout=60).run.status == RunStatus.SUCCEEDED
    assert file_reads(ctx, run_id) == reads


def test_expired_lease_is_reconciled_by_reattaching(
    ctx: AppContext, fake_herdr: FakeHerdr, monkeypatch: pytest.MonkeyPatch, evidence_project: str
) -> None:
    """A crashed worker (no shutdown path) leaves a stale lease; reconciliation reattaches."""
    from agenttape.worker.executors import agent as agent_executor

    with monkeypatch.context() as crash:
        # Simulate a crash: the shutdown path neither reattaches nor finalizes.
        crash.setattr(agent_executor, "reattach_or_none", lambda *_a: True)
        run_id = start_demo(ctx, evidence_project, step_delay_ms=SLOW_MS)
        worker = BackgroundWorker(ctx).start()
        wait_until(has_telemetry(ctx, run_id), what="telemetry")
        worker.shutdown()
    assert RunService(ctx).get_run(run_id).status == RunStatus.RUNNING  # left behind
    with ctx.session() as session:
        session.execute(
            update(RunAttempt)
            .where(RunAttempt.run_id == run_id)
            .values(lease_expires_at=ctx.clock.now() - timedelta(seconds=1))
        )
    report = reconcile_stale(ctx)
    assert len(report["reattach"]) == 1 and report["interrupted"] == []
    detail = drive(ctx, run_id, timeout=60)
    assert detail.run.status == RunStatus.WAITING_FOR_APPROVAL, detail.run.failure_message
    assert file_reads(ctx, run_id) == telemetry_reads(ctx, run_id)


def test_reattach_to_a_dead_session_is_a_structured_failure(
    ctx: AppContext, fake_herdr: FakeHerdr, evidence_project: str
) -> None:
    run_id = start_demo(ctx, evidence_project, step_delay_ms=SLOW_MS)
    worker = BackgroundWorker(ctx).start()
    wait_until(has_telemetry(ctx, run_id), what="telemetry")
    worker.shutdown()
    fake_herdr.close_pane(runtime_row(ctx, run_id).pane_id or "")  # Herdr lost it meanwhile
    detail = drive(ctx, run_id)
    assert detail.run.status == RunStatus.FAILED
    row = runtime_row(ctx, run_id)
    assert row.status == "failed" and row.failure_category == "stale_id"


# --------------------------------------------------------------------------- API


def test_runtime_api_shows_refs_and_attach_command_without_socket_paths(
    fake_herdr: FakeHerdr, ctx: AppContext, client: TestClient
) -> None:
    run_id = create_run(ctx, herdr_profile(ctx, agent_kind="claude"))
    drive(ctx, run_id)
    body = client.get(f"/api/v1/runs/{run_id}/runtime").json()
    assert body["runtime"] == "herdr"
    [session] = body["sessions"]
    assert session["attempt_number"] == 1 and session["agent_kind"] == "claude"
    assert session["native_session_ref"] == "reported (id)"
    assert session["attach"]["command"] == f"herdr agent attach {session['agent_name']}"
    assert {c["name"] for c in session["capabilities"]} >= {"reconnect", "attach"}
    assert "h.sock" not in str(body) and "fake-session-secret-id" not in str(body)

    status = client.get("/api/v1/runtime/herdr").json()
    assert status["reachable"] and status["version"] == "0.9.0-fake" and status["protocol"] == 22
    assert status["installed"] is False and status["machines"] is None
    assert {i["target"] for i in status["integrations"]} == {"claude", "codex"}
    assert "herdr:claude" in status["runtime_capabilities"]
    assert status["defaults"] == {"workspace_policy": "new", "cleanup": "keep", "fallback": "none"}
    assert "h.sock" not in str(status)

    test = client.post("/api/v1/runtime/herdr/test").json()
    by_name = {c["name"]: c for c in test["checks"]}
    assert by_name["Server answers ping"]["ok"] and by_name["Event subscription"]["ok"]
    assert by_name["Herdr CLI installed"]["ok"] is False  # tests never use the real CLI
    assert test["ok"] is False


def test_runtime_api_when_herdr_is_unavailable(client: TestClient) -> None:
    status = client.get("/api/v1/runtime/herdr").json()
    assert status["reachable"] is False and "unavailable" in status["message"]
    test = client.post("/api/v1/runtime/herdr/test").json()
    ping = next(c for c in test["checks"] if c["name"] == "Server answers ping")
    assert ping["ok"] is False and ping["hint"]
    assert "no-herdr.sock" not in str(status) + str(test)


def test_remote_machine_refs_without_ssh(
    fake_herdr: FakeHerdr, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A socket that reaches a saved machine is labelled; AgentTape never runs SSH."""
    from agenttape.api.app import create_app
    from agenttape.bootstrap import build_context
    from tests.conftest import make_settings

    log = tmp_path / "herdr-cli.log"
    cli = tmp_path / "herdr"
    machines = '[{"label":"buildbox","ssh_target":"me@buildbox","enabled":true,"token":"x"}]'
    cli.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> {log}\n'
        'if [ "$1" = "--version" ]; then echo "herdr 0.9.0"; exit 0; fi\n'
        f'if [ "$1" = "machine" ]; then echo \'{machines}\'; exit 0; fi\n'
        "exit 2\n"
    )
    cli.chmod(0o755)
    monkeypatch.setenv("AGENTTAPE_HERDR_EXECUTABLE", str(cli))
    monkeypatch.setenv("AGENTTAPE_HERDR_MACHINE", "buildbox")
    remote = tmp_path / "remote"
    remote.mkdir()
    remote_ctx = build_context(make_settings(remote), migrate=True)
    try:
        run_id = create_run(remote_ctx, herdr_profile(remote_ctx, agent_kind="claude"))
        assert drive(remote_ctx, run_id).run.status == RunStatus.SUCCEEDED
        assert runtime_row(remote_ctx, run_id).machine == "buildbox"
        app = create_app(remote_ctx, web_dist=tmp_path / "none")
        with TestClient(app, base_url="http://127.0.0.1") as http:
            view = http.get(f"/api/v1/runs/{run_id}/runtime").json()["sessions"][0]
            status = http.get("/api/v1/runtime/herdr").json()
    finally:
        remote_ctx.close()
    assert view["machine"] == "buildbox" and view["attach"]["command"] is None
    assert "buildbox" in view["attach"]["guidance"]
    assert status["machine"] == "buildbox" and status["cli_version"] == "0.9.0"
    assert status["machines"] == [
        {"label": "buildbox", "ssh_target": "me@buildbox", "enabled": True}
    ]
    assert set(log.read_text().splitlines()) <= {"--version", "machine list --json"}
