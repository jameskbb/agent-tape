from __future__ import annotations

from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from agenttape.infrastructure.db import migrate
from agenttape.infrastructure.db.base import Base
from agenttape.infrastructure.db.engine import make_engine

EXPECTED_TABLES = {
    "workspaces",
    "projects",
    "agent_profiles",
    "agent_profile_versions",
    "prompt_templates",
    "prompt_versions",
    "workflows",
    "workflow_versions",
    "workflow_nodes",
    "workflow_edges",
    "schedules",
    "schedule_occurrences",
    "triggers",
    "trigger_deliveries",
    "runs",
    "run_attempts",
    "step_runs",
    "events",
    "artifacts",
    "file_changes",
    "evaluation_definitions",
    "evaluation_definition_versions",
    "evaluation_suites",
    "evaluation_results",
    "approval_requests",
    "worker_heartbeats",
    "audit_entries",
    "secret_references",
    "queue_items",
    "scheduler_leases",
    "app_settings",
    "findings",
    "citations",
    "citation_verdicts",
    "observed_sessions",
    "evidence_blobs",
    "evidence_reads",
    "citation_reproductions",
}


def test_upgrade_downgrade_upgrade(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'm.db'}"
    migrate.upgrade(url)
    engine = create_engine(url)
    assert set(inspect(engine).get_table_names()) >= EXPECTED_TABLES
    migrate.downgrade(url, "base")
    assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
    migrate.upgrade(url)
    assert set(inspect(engine).get_table_names()) >= EXPECTED_TABLES
    assert migrate.is_up_to_date(engine, url)


def test_models_match_migrations(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'drift.db'}"
    migrate.upgrade(url)
    with create_engine(url).connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], f"models and migrations differ: {diff}"


def test_one_head_descends_from_core(tmp_path: Path) -> None:
    """Every revision chains back to 0001_core and parallel branches are merged: one head."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(migrate.alembic_config(f"sqlite:///{tmp_path / 'h.db'}"))
    assert script.get_bases() == ["0001_core"]
    heads = script.get_heads()
    assert len(heads) == 1, f"expected one migration head, found {heads}"
    ancestors = {rev.revision for rev in script.iterate_revisions(heads[0], "base")}
    assert "0001_core" in ancestors


def test_sqlite_pragmas(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'p.db'}")
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert int(conn.execute(text("PRAGMA busy_timeout")).scalar() or 0) >= 5000


def test_idempotency_and_occurrence_constraints_exist(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'c.db'}"
    migrate.upgrade(url)
    insp = inspect(create_engine(url))
    runs = {tuple(u["column_names"]) for u in insp.get_unique_constraints("runs")}
    assert ("source", "idempotency_key") in runs
    occ = {tuple(u["column_names"]) for u in insp.get_unique_constraints("schedule_occurrences")}
    assert ("schedule_id", "occurrence_at") in occ
    events = {tuple(u["column_names"]) for u in insp.get_unique_constraints("events")}
    assert ("run_id", "sequence") in events
