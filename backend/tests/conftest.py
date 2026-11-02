"""Shared fixtures. Every test gets its own temporary data dir and SQLite database."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect

from agenttape.api.app import create_app
from agenttape.application.context import AppContext
from agenttape.application.schemas.catalog import AgentProfileCreate, PromptTemplateCreate
from agenttape.application.schemas.runs import RunCreate, RunDetail
from agenttape.application.services.profiles import AgentProfileService
from agenttape.application.services.prompts import PromptService
from agenttape.application.services.runs import RunService
from agenttape.bootstrap import build_context
from agenttape.config import Settings
from agenttape.domain.enums import TERMINAL_STATUSES, RunStatus
from agenttape.domain.prompts import VariableDefinition
from agenttape.worker.worker import Worker

#: Set to e.g. postgresql+psycopg://user:pass@localhost/agenttape_test to run the suite
#: against PostgreSQL (see docs/DEVELOPMENT.md). Each test starts from an empty schema.
TEST_DATABASE_URL = os.environ.get("AGENTTAPE_TEST_DATABASE_URL") or None


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    repos = tmp_path / "repos"
    repos.mkdir(exist_ok=True)
    values: dict[str, Any] = {
        "data_dir": tmp_path / "data",
        "demo_step_delay_ms": 0,
        "lease_seconds": 5.0,
        "heartbeat_interval_seconds": 0.5,
        "worker_poll_interval_seconds": 0.05,
        "retry_backoff_base_seconds": 0.01,
        "retry_backoff_max_seconds": 0.05,
        "cancel_grace_seconds": 1.0,
        "allowed_roots": [repos],
        # SQLite in the temp data dir, unless CI points the suite at PostgreSQL.
        "database_url": TEST_DATABASE_URL,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture(autouse=True)
def _never_real_agent_clis(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Tests never discover or launch the real Claude Code or Codex CLIs."""
    missing = str(tmp_path / "no-such-cli")
    monkeypatch.setenv("AGENTTAPE_CLAUDE_EXECUTABLE", missing)
    monkeypatch.setenv("AGENTTAPE_CODEX_EXECUTABLE", missing)
    monkeypatch.delenv("AGENTTAPE_ALLOWED_EXECUTABLES", raising=False)
    # ...nor a real Herdr session or CLI (M2; Herdr tests use tests/fixtures/fake_herdr).
    monkeypatch.setenv("AGENTTAPE_HERDR_SOCKET", str(tmp_path / "no-herdr.sock"))
    monkeypatch.setenv("AGENTTAPE_HERDR_EXECUTABLE", missing)
    monkeypatch.delenv("AGENTTAPE_HERDR_SESSION", raising=False)


@pytest.fixture(autouse=True)
def _empty_postgresql_schema() -> None:
    """On PostgreSQL every test shares one database, so each starts from an empty schema,
    including tests that build their own context instead of using ``ctx``."""
    if not TEST_DATABASE_URL:
        return
    engine = create_engine(TEST_DATABASE_URL)
    try:
        dirty = bool(set(inspect(engine).get_table_names()) - {"alembic_version"})
    finally:
        engine.dispose()
    if dirty:
        from agenttape.infrastructure.db.migrate import downgrade

        downgrade(TEST_DATABASE_URL, "base")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def ctx(settings: Settings) -> Iterator[AppContext]:
    if TEST_DATABASE_URL:
        from agenttape.infrastructure.db.migrate import downgrade

        downgrade(TEST_DATABASE_URL, "base")  # a clean schema per test
    context = build_context(settings, migrate=True)
    yield context
    context.close()


@pytest.fixture
def client(ctx: AppContext) -> Iterator[TestClient]:
    app = create_app(ctx, web_dist=ctx.settings.data_dir / "no-web-dist")
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client


def demo_profile(
    ctx: AppContext, scenario: str = "success", name: str | None = None, **config: Any
) -> str:
    cfg: dict[str, Any] = {"scenario": scenario, **config}
    return (
        AgentProfileService(ctx)
        .create(
            AgentProfileCreate(name=name or f"demo-{scenario}", adapter_type="demo", config=cfg)
        )
        .id
    )


def simple_prompt(
    ctx: AppContext, name: str = "Fix things", body: str = "Please fix {{ thing }}."
) -> str:
    return (
        PromptService(ctx)
        .create_template(
            PromptTemplateCreate(
                name=name, body=body, variables=[VariableDefinition(name="thing", default="bugs")]
            )
        )
        .id
    )


def start_run(ctx: AppContext, profile_id: str, **kwargs: Any) -> str:
    return (
        RunService(ctx)
        .create_run(RunCreate(agent_profile_id=profile_id, prompt="Do the thing.", **kwargs))
        .run.id
    )


def drive(
    ctx: AppContext,
    run_id: str,
    *,
    timeout: float = 30.0,
    stop_on_wait: bool = True,
    worker_id: str = "test-worker",
) -> RunDetail:
    """Execute queued work in this thread until ``run_id`` settles."""
    worker = Worker(ctx, worker_id=worker_id, concurrency=1)
    deadline = time.monotonic() + timeout
    service = RunService(ctx)
    while time.monotonic() < deadline:
        asyncio.run(worker.run_until_idle())
        status = RunStatus(service.get_run(run_id).status)
        if status in TERMINAL_STATUSES or (
            stop_on_wait and status == RunStatus.WAITING_FOR_APPROVAL
        ):
            return service.get_detail(run_id)
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} did not settle; status {service.get_run(run_id).status}")


def event_types(ctx: AppContext, run_id: str) -> list[str]:
    return [e.type for e in RunService(ctx).list_events(run_id, limit=1000).items]
