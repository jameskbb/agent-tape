"""M3: observed sessions (interactive Claude Code capture).

``observed_sessions`` maps a Claude Code session AgentTape did not (necessarily) start to
a run: one row per (session, run), keyed on the session id's ``sha256:`` hash (the raw id
is never stored). Branches off ``m3F_0001`` (findings), as does the verification branch
(``m3V_0001``, ``m3V_0002``); ``m3_0002`` merges the two into one head.

Revision ID: m3C_0001
Revises: m3F_0001
Create Date: 2026-09-26 12:46:17.746578
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

import agenttape.infrastructure.db.base

revision: str = "m3C_0001"
down_revision: str | Sequence[str] | None = "m3F_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "observed_sessions",
        sa.Column("session_hash", sa.String(length=71), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=True),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("agent_kind", sa.String(length=32), nullable=False),
        sa.Column("cwd", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "first_seen_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "closed_at", agenttape.infrastructure.db.base.UTCDateTime(timezone=True), nullable=True
        ),
        sa.Column("close_reason", sa.String(length=32), nullable=True),
        sa.Column("mcp_instance", sa.String(length=64), nullable=True),
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
            name=op.f("fk_observed_sessions_attempt_id_run_attempts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_observed_sessions_project_id_projects"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_observed_sessions_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_observed_sessions")),
        sa.UniqueConstraint(
            "session_hash", "run_id", name=op.f("uq_observed_sessions_session_hash_run_id")
        ),
    )
    with op.batch_alter_table("observed_sessions", schema=None) as batch_op:
        batch_op.create_index(
            "ix_observed_sessions_hash_status", ["session_hash", "status"], unique=False
        )
        batch_op.create_index(
            "ix_observed_sessions_status_seen", ["status", "last_seen_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("observed_sessions", schema=None) as batch_op:
        batch_op.drop_index("ix_observed_sessions_status_seen")
        batch_op.drop_index("ix_observed_sessions_hash_status")

    op.drop_table("observed_sessions")
