"""M3: retained evidence and citation reproductions.

Two tables the verification engine writes through (``evidence_blobs``, one row per
(run, sha256) of retained read content; ``evidence_reads``, one row per read verification
used, pointing at the blob its content lives in) and one the reproduction service fills
(``citation_reproductions``, re-running the command or search behind a citation).

Branches off ``m3F_0001`` (findings), a sibling of ``m3C_0001`` (observed
sessions) off the same ancestor; ``m3_0002`` merges the two heads.

Revision ID: m3V_0001
Revises: m3F_0001
Create Date: 2026-09-26 18:42:46.434093
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

import agenttape.infrastructure.db.base

revision: str = "m3V_0001"
down_revision: str | Sequence[str] | None = "m3F_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "evidence_blobs",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("git_blob", sa.String(length=40), nullable=True),
        sa.Column("storage_path", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("redacted", sa.Boolean(), nullable=False),
        sa.Column("media_type", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_evidence_blobs_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evidence_blobs")),
        sa.UniqueConstraint("run_id", "sha256", name=op.f("uq_evidence_blobs_run_id_sha256")),
    )

    op.create_table(
        "evidence_reads",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("result_event_id", sa.String(length=36), nullable=False),
        sa.Column("call_event_id", sa.String(length=36), nullable=True),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("line_first", sa.Integer(), nullable=True),
        sa.Column("line_last", sa.Integer(), nullable=True),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("redacted", sa.Boolean(), nullable=False),
        sa.Column("read_hash", sa.String(length=40), nullable=True),
        sa.Column("blob_id", sa.String(length=36), nullable=True),
        sa.Column(
            "created_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["blob_id"],
            ["evidence_blobs.id"],
            name=op.f("fk_evidence_reads_blob_id_evidence_blobs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["call_event_id"],
            ["events.id"],
            name=op.f("fk_evidence_reads_call_event_id_events"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["result_event_id"],
            ["events.id"],
            name=op.f("fk_evidence_reads_result_event_id_events"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_evidence_reads_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evidence_reads")),
        sa.UniqueConstraint("result_event_id", name=op.f("uq_evidence_reads_result_event_id")),
    )
    with op.batch_alter_table("evidence_reads", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_evidence_reads_blob_id"), ["blob_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_evidence_reads_call_event_id"), ["call_event_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_evidence_reads_result_event_id"),
            ["result_event_id"],
            unique=False,
        )
        batch_op.create_index("ix_evidence_reads_run_path", ["run_id", "path"], unique=False)

    op.create_table(
        "citation_reproductions",
        sa.Column("citation_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("source_event_id", sa.String(length=36), nullable=True),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("requested_by", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("result", sa.String(length=16), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "argv",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("cwd", sa.Text(), nullable=True),
        sa.Column("pinned_commit", sa.String(length=64), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("recorded_exit_code", sa.Integer(), nullable=True),
        sa.Column("output_excerpt", sa.Text(), nullable=True),
        sa.Column("output_sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "evidence",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "finished_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["citation_id"],
            ["citations.id"],
            name=op.f("fk_citation_reproductions_citation_id_citations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_citation_reproductions_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_event_id"],
            ["events.id"],
            name=op.f("fk_citation_reproductions_source_event_id_events"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_citation_reproductions")),
    )
    with op.batch_alter_table("citation_reproductions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_citation_reproductions_citation_id"), ["citation_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_citation_reproductions_run_id"), ["run_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_citation_reproductions_source_event_id"),
            ["source_event_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("citation_reproductions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_citation_reproductions_source_event_id"))
        batch_op.drop_index(batch_op.f("ix_citation_reproductions_run_id"))
        batch_op.drop_index(batch_op.f("ix_citation_reproductions_citation_id"))

    op.drop_table("citation_reproductions")
    with op.batch_alter_table("evidence_reads", schema=None) as batch_op:
        batch_op.drop_index("ix_evidence_reads_run_path")
        batch_op.drop_index(batch_op.f("ix_evidence_reads_result_event_id"))
        batch_op.drop_index(batch_op.f("ix_evidence_reads_call_event_id"))
        batch_op.drop_index(batch_op.f("ix_evidence_reads_blob_id"))

    op.drop_table("evidence_reads")
    op.drop_table("evidence_blobs")
