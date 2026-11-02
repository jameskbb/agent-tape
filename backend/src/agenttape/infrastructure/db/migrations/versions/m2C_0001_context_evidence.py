"""M2: context sources, packs, evidence events, evidence links, and feedback.

Revision ID: m2C_0001
Revises: trackE_0001
Create Date: 2026-09-23 22:39:53.219324
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

import agenttape.infrastructure.db.base

revision: str = "m2C_0001"
down_revision: str | Sequence[str] | None = "trackE_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:

    op.create_table(
        "context_sources",
        sa.Column("scope", sa.String(length=64), nullable=False),
        sa.Column("source_key", sa.String(length=300), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=True),
        sa.Column("locator", sa.Text(), nullable=True),
        sa.Column("authority", sa.String(length=32), nullable=False),
        sa.Column("live", sa.Boolean(), nullable=True),
        sa.Column("version", sa.String(length=200), nullable=True),
        sa.Column("synced_at", sa.String(length=64), nullable=True),
        sa.Column("sensitivity", sa.String(length=32), nullable=False),
        sa.Column("retention", sa.String(length=64), nullable=False),
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
            "details",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_context_sources")),
        sa.UniqueConstraint(
            "scope", "source_key", name=op.f("uq_context_sources_scope_source_key")
        ),
    )
    with op.batch_alter_table("context_sources", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_context_sources_source_type"), ["source_type"], unique=False
        )

    op.create_table(
        "context_feedback",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("dedupe_key", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("source_key", sa.String(length=300), nullable=True),
        sa.Column("event_id", sa.String(length=36), nullable=True),
        sa.Column("reported_by", sa.String(length=32), nullable=False),
        sa.Column("suggested_classification", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("classification", sa.String(length=32), nullable=True),
        sa.Column("proposed_destination", sa.String(length=32), nullable=True),
        sa.Column("lesson_text", sa.Text(), nullable=True),
        sa.Column("lesson_version", sa.Integer(), nullable=True),
        sa.Column("reviewed_by", sa.String(length=200), nullable=True),
        sa.Column(
            "reviewed_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("review_note", sa.Text(), nullable=True),
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
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_context_feedback_project_id_projects"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_context_feedback_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_context_feedback")),
        sa.UniqueConstraint(
            "run_id", "dedupe_key", name=op.f("uq_context_feedback_run_id_dedupe_key")
        ),
    )
    with op.batch_alter_table("context_feedback", schema=None) as batch_op:
        batch_op.create_index(
            "ix_context_feedback_project_status", ["project_id", "status"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_context_feedback_run_id"), ["run_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_context_feedback_status"), ["status"], unique=False)

    op.create_table(
        "evidence_links",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("subject_type", sa.String(length=32), nullable=False),
        sa.Column("subject_key", sa.String(length=200), nullable=False),
        sa.Column("subject_text", sa.Text(), nullable=True),
        sa.Column("source_id", sa.String(length=36), nullable=True),
        sa.Column("source_key", sa.String(length=300), nullable=False),
        sa.Column("relation", sa.String(length=16), nullable=False),
        sa.Column("origin", sa.String(length=32), nullable=False),
        sa.Column("event_id", sa.String(length=36), nullable=True),
        sa.Column(
            "created_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_evidence_links_run_id_runs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["context_sources.id"],
            name=op.f("fk_evidence_links_source_id_context_sources"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evidence_links")),
        sa.UniqueConstraint(
            "run_id",
            "subject_key",
            "source_key",
            "relation",
            name=op.f("uq_evidence_links_run_id_subject_key_source_key_relation"),
        ),
    )
    with op.batch_alter_table("evidence_links", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_evidence_links_run_id"), ["run_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_evidence_links_source_id"), ["source_id"], unique=False
        )

    op.create_table(
        "context_evidence_events",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=True),
        sa.Column("node_key", sa.String(length=100), nullable=True),
        sa.Column("event_id", sa.String(length=36), nullable=True),
        sa.Column("dedupe_key", sa.String(length=200), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "ts", agenttape.infrastructure.db.base.UTCDateTime(timezone=True), nullable=False
        ),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=36), nullable=True),
        sa.Column("source_key", sa.String(length=300), nullable=True),
        sa.Column("source_type", sa.String(length=64), nullable=True),
        sa.Column("source_name", sa.String(length=300), nullable=True),
        sa.Column("provider", sa.String(length=100), nullable=True),
        sa.Column("authority", sa.String(length=32), nullable=True),
        sa.Column("live", sa.Boolean(), nullable=True),
        sa.Column("freshness", sa.String(length=16), nullable=False),
        sa.Column("version", sa.String(length=200), nullable=True),
        sa.Column("synced_at", sa.String(length=64), nullable=True),
        sa.Column("quality", sa.String(length=32), nullable=False),
        sa.Column("query", sa.Text(), nullable=True),
        sa.Column(
            "tool",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column(
            "result",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column("reason", sa.Text(), nullable=True),
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
            name=op.f("fk_context_evidence_events_attempt_id_run_attempts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_context_evidence_events_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["context_sources.id"],
            name=op.f("fk_context_evidence_events_source_id_context_sources"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_context_evidence_events")),
        sa.UniqueConstraint(
            "run_id", "dedupe_key", name=op.f("uq_context_evidence_events_run_id_dedupe_key")
        ),
    )
    with op.batch_alter_table("context_evidence_events", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_context_evidence_events_attempt_id"), ["attempt_id"], unique=False
        )
        batch_op.create_index(
            "ix_context_evidence_events_run_seq", ["run_id", "sequence"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_context_evidence_events_source_id"), ["source_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_context_evidence_events_stage"), ["stage"], unique=False
        )

    op.create_table(
        "context_packs",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("node_key", sa.String(length=100), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("policy", sa.String(length=64), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("token_estimate", sa.Integer(), nullable=True),
        sa.Column("token_method", sa.String(length=100), nullable=True),
        sa.Column(
            "items",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column(
            "excluded",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column(
            "redaction",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("injected_text", sa.Text(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["run_attempts.id"],
            name=op.f("fk_context_packs_attempt_id_run_attempts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_context_packs_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_context_packs")),
        sa.UniqueConstraint("attempt_id", name=op.f("uq_context_packs_attempt_id")),
        sa.UniqueConstraint("run_id", "version", name=op.f("uq_context_packs_run_id_version")),
    )
    with op.batch_alter_table("context_packs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_context_packs_run_id"), ["run_id"], unique=False)


def downgrade() -> None:

    with op.batch_alter_table("context_packs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_context_packs_run_id"))

    op.drop_table("context_packs")
    with op.batch_alter_table("context_evidence_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_context_evidence_events_stage"))
        batch_op.drop_index(batch_op.f("ix_context_evidence_events_source_id"))
        batch_op.drop_index("ix_context_evidence_events_run_seq")
        batch_op.drop_index(batch_op.f("ix_context_evidence_events_attempt_id"))

    op.drop_table("context_evidence_events")
    with op.batch_alter_table("evidence_links", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_evidence_links_source_id"))
        batch_op.drop_index(batch_op.f("ix_evidence_links_run_id"))

    op.drop_table("evidence_links")
    with op.batch_alter_table("context_feedback", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_context_feedback_status"))
        batch_op.drop_index(batch_op.f("ix_context_feedback_run_id"))
        batch_op.drop_index("ix_context_feedback_project_status")

    op.drop_table("context_feedback")
    with op.batch_alter_table("context_sources", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_context_sources_source_type"))

    op.drop_table("context_sources")
