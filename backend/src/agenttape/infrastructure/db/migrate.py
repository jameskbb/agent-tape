"""Programmatic Alembic helpers used by the CLI, the app, and tests.

Parallel branches of work add revisions that branch from a shared head and a merge
revision reunites them. Upgrades therefore always target ``heads`` so an unmerged multi-head state still
migrates fully.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    cfg.attributes["configure_logging"] = False
    return cfg


def upgrade(url: str, revision: str = "heads") -> None:
    command.upgrade(alembic_config(url), revision)


def downgrade(url: str, revision: str = "base") -> None:
    command.downgrade(alembic_config(url), revision)


def script_heads(url: str) -> set[str]:
    return set(ScriptDirectory.from_config(alembic_config(url)).get_heads())


def current_revisions(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        return set(MigrationContext.configure(conn).get_current_heads())


def is_up_to_date(engine: Engine, url: str) -> bool:
    return current_revisions(engine) == script_heads(url)
