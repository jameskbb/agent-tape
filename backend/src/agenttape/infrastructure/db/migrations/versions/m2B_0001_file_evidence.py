"""M2: File Evidence (file_evidence_events, file_evidence_snapshots, run_file_summaries).

Revision ID: m2B_0001
Revises: trackE_0001
Create Date: 2026-09-23 22:42:43.707601
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

import agenttape.infrastructure.db.base

revision: str = "m2B_0001"
down_revision: str | Sequence[str] | None = "trackE_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "run_file_summaries",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("file_key", sa.Text(), nullable=False),
        sa.Column("start_path", sa.Text(), nullable=True),
        sa.Column("end_path", sa.Text(), nullable=True),
        sa.Column(
            "rename_chain",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column(
            "first_at", agenttape.infrastructure.db.base.UTCDateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "last_at", agenttape.infrastructure.db.base.UTCDateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "operation_counts",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("operation_total", sa.Integer(), nullable=False),
        sa.Column("first_step", sa.String(length=100), nullable=True),
        sa.Column("last_step", sa.String(length=100), nullable=True),
        sa.Column(
            "attempts",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column(
            "agents",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column(
            "tools",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column(
            "sources",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("classification", sa.String(length=24), nullable=False),
        sa.Column(
            "classifications",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("start_hash", sa.String(length=64), nullable=True),
        sa.Column("end_hash", sa.String(length=64), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("is_binary", sa.Boolean(), nullable=True),
        sa.Column("net_status", sa.String(length=16), nullable=False),
        sa.Column("insertions", sa.Integer(), nullable=True),
        sa.Column("deletions", sa.Integer(), nullable=True),
        sa.Column("net_unchanged", sa.Boolean(), nullable=False),
        sa.Column("is_protected", sa.Boolean(), nullable=False),
        sa.Column(
            "protected_patterns",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("is_ignored", sa.Boolean(), nullable=False),
        sa.Column("is_generated", sa.Boolean(), nullable=False),
        sa.Column("is_outside", sa.Boolean(), nullable=False),
        sa.Column("is_collapsed", sa.Boolean(), nullable=False),
        sa.Column("collapsed_count", sa.Integer(), nullable=True),
        sa.Column("completeness", sa.String(length=24), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column(
            "policy",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column(
            "rebuilt_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_run_file_summaries_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_file_summaries")),
        sa.UniqueConstraint(
            "run_id", "file_key", name=op.f("uq_run_file_summaries_run_id_file_key")
        ),
    )
    with op.batch_alter_table("run_file_summaries", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_run_file_summaries_classification"), ["classification"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_run_file_summaries_run_id"), ["run_id"], unique=False)

    op.create_table(
        "file_evidence_events",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=True),
        sa.Column("node_key", sa.String(length=100), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column(
            "ts", agenttape.infrastructure.db.base.UTCDateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "last_ts", agenttape.infrastructure.db.base.UTCDateTime(timezone=True), nullable=True
        ),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("canonical_path", sa.Text(), nullable=True),
        sa.Column("previous_path", sa.Text(), nullable=True),
        sa.Column("inside_boundary", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column("quality", sa.String(length=16), nullable=False),
        sa.Column("adapter", sa.String(length=64), nullable=True),
        sa.Column("runtime", sa.String(length=32), nullable=True),
        sa.Column("tool", sa.String(length=200), nullable=True),
        sa.Column("command", sa.Text(), nullable=True),
        sa.Column("correlation_id", sa.String(length=100), nullable=True),
        sa.Column("source_event_id", sa.String(length=36), nullable=True),
        sa.Column("before_hash", sa.String(length=64), nullable=True),
        sa.Column("after_hash", sa.String(length=64), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("is_binary", sa.Boolean(), nullable=True),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column(
            "protected_patterns",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column(
            "policy",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column("redacted", sa.Boolean(), nullable=False),
        sa.Column(
            "details",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["run_attempts.id"],
            name=op.f("fk_file_evidence_events_attempt_id_run_attempts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_file_evidence_events_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_file_evidence_events")),
        sa.UniqueConstraint(
            "run_id", "dedupe_key", name=op.f("uq_file_evidence_events_run_id_dedupe_key")
        ),
        sa.UniqueConstraint(
            "run_id", "sequence", name=op.f("uq_file_evidence_events_run_id_sequence")
        ),
    )
    with op.batch_alter_table("file_evidence_events", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_file_evidence_events_attempt_id"), ["attempt_id"], unique=False
        )
        batch_op.create_index(
            "ix_file_evidence_events_run_id_path", ["run_id", "path"], unique=False
        )

    op.create_table(
        "file_evidence_snapshots",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column(
            "taken_at", agenttape.infrastructure.db.base.UTCDateTime(timezone=True), nullable=False
        ),
        sa.Column("is_repo", sa.Boolean(), nullable=False),
        sa.Column("head", sa.String(length=64), nullable=True),
        sa.Column("branch", sa.String(length=255), nullable=True),
        sa.Column("dirty", sa.Boolean(), nullable=True),
        sa.Column(
            "files",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column(
            "details",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["run_attempts.id"],
            name=op.f("fk_file_evidence_snapshots_attempt_id_run_attempts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_file_evidence_snapshots_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_file_evidence_snapshots")),
        sa.UniqueConstraint(
            "attempt_id", "kind", name=op.f("uq_file_evidence_snapshots_attempt_id_kind")
        ),
    )
    with op.batch_alter_table("file_evidence_snapshots", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_file_evidence_snapshots_run_id"), ["run_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("file_evidence_snapshots", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_file_evidence_snapshots_run_id"))

    op.drop_table("file_evidence_snapshots")
    with op.batch_alter_table("file_evidence_events", schema=None) as batch_op:
        batch_op.drop_index("ix_file_evidence_events_run_id_path")
        batch_op.drop_index(batch_op.f("ix_file_evidence_events_attempt_id"))

    op.drop_table("file_evidence_events")
    with op.batch_alter_table("run_file_summaries", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_run_file_summaries_run_id"))
        batch_op.drop_index(batch_op.f("ix_run_file_summaries_classification"))

    op.drop_table("run_file_summaries")
