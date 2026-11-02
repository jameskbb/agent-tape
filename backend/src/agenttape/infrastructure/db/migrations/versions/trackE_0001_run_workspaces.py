"""run_workspaces, the per-run workdir and worktree ownership record.

Revision ID: trackE_0001
Revises: 0001_core
Create Date: 2026-09-23 13:58:34.216221
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

import agenttape.infrastructure.db.base

revision: str = "trackE_0001"
down_revision: str | Sequence[str] | None = "0001_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "run_workspaces",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("repo_path", sa.Text(), nullable=True),
        sa.Column("path", sa.Text(), nullable=True),
        sa.Column("isolated", sa.Boolean(), nullable=False),
        sa.Column("owned", sa.Boolean(), nullable=False),
        sa.Column("branch", sa.String(length=255), nullable=True),
        sa.Column("base_commit", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("adapter_type", sa.String(length=64), nullable=True),
        sa.Column("adapter_version", sa.String(length=100), nullable=True),
        sa.Column("last_commit_sha", sa.String(length=64), nullable=True),
        sa.Column(
            "removed_at", agenttape.infrastructure.db.base.UTCDateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "details",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_run_workspaces_project_id_projects"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_run_workspaces_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_workspaces")),
        sa.UniqueConstraint("run_id", name=op.f("uq_run_workspaces_run_id")),
    )
    with op.batch_alter_table("run_workspaces", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_run_workspaces_is_demo"), ["is_demo"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_run_workspaces_project_id"), ["project_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_run_workspaces_status"), ["status"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("run_workspaces", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_run_workspaces_status"))
        batch_op.drop_index(batch_op.f("ix_run_workspaces_project_id"))
        batch_op.drop_index(batch_op.f("ix_run_workspaces_is_demo"))

    op.drop_table("run_workspaces")
