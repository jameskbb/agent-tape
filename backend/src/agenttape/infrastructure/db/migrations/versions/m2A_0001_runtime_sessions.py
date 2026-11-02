"""M2: runtime_sessions, the Herdr identity of each attempt (for reattach).

Revision ID: m2A_0001
Revises: trackE_0001
Create Date: 2026-09-23 22:41:29.333493
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

import agenttape.infrastructure.db.base

revision: str = "m2A_0001"
down_revision: str | Sequence[str] | None = "trackE_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "runtime_sessions",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("runtime", sa.String(length=32), nullable=False),
        sa.Column("machine", sa.String(length=200), nullable=False),
        sa.Column("session_name", sa.String(length=200), nullable=True),
        sa.Column("herdr_version", sa.String(length=64), nullable=True),
        sa.Column("protocol", sa.Integer(), nullable=True),
        sa.Column("workspace_id", sa.String(length=64), nullable=True),
        sa.Column("tab_id", sa.String(length=64), nullable=True),
        sa.Column("pane_id", sa.String(length=64), nullable=True),
        sa.Column("agent_name", sa.String(length=64), nullable=True),
        sa.Column("agent_kind", sa.String(length=32), nullable=True),
        sa.Column("native_session_kind", sa.String(length=16), nullable=True),
        sa.Column("lifecycle_state", sa.String(length=16), nullable=True),
        sa.Column("owned_workspace", sa.Boolean(), nullable=False),
        sa.Column("owned_agent", sa.Boolean(), nullable=False),
        sa.Column("telemetry_offset", sa.Integer(), nullable=False),
        sa.Column(
            "last_sync_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("failure_category", sa.String(length=32), nullable=True),
        sa.Column(
            "details",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
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
            ["attempt_id"],
            ["run_attempts.id"],
            name=op.f("fk_runtime_sessions_attempt_id_run_attempts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_runtime_sessions_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runtime_sessions")),
        sa.UniqueConstraint("attempt_id", name=op.f("uq_runtime_sessions_attempt_id")),
    )
    with op.batch_alter_table("runtime_sessions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_runtime_sessions_run_id"), ["run_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_runtime_sessions_status"), ["status"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("runtime_sessions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_runtime_sessions_status"))
        batch_op.drop_index(batch_op.f("ix_runtime_sessions_run_id"))

    op.drop_table("runtime_sessions")
