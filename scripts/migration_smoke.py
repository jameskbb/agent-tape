#!/usr/bin/env python3
"""Migration smoke test: upgrade a fresh SQLite database to heads, downgrade to base,
upgrade again, and confirm the models match the migrated schema (no drift)."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect

import agenttape.infrastructure.db.models  # noqa: F401
from agenttape.infrastructure.db import migrate
from agenttape.infrastructure.db.base import Base


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite:///{Path(tmp) / 'smoke.db'}"
        migrate.upgrade(url)
        engine = create_engine(url)
        tables = set(inspect(engine).get_table_names())
        migrate.downgrade(url, "base")
        left = set(inspect(engine).get_table_names()) - {"alembic_version"}
        if left:
            print(f"migration smoke: downgrade left tables behind: {sorted(left)}")
            return 1
        migrate.upgrade(url)
        with engine.connect() as conn:
            diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
        engine.dispose()
        if diff:
            print(f"migration smoke: models differ from migrations: {diff}")
            return 1
        heads = migrate.script_heads(url)
        print(f"migration smoke: ok ({len(tables) - 1} tables, heads {sorted(heads)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
