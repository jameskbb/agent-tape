"""M3: findings, citations, citation verdicts.

The findings and citations schema that the rest of M3 reads or writes through
``FindingsService``: ``findings`` (one row per FINDING event), ``citations`` (a finding's
declared file line ranges and quotes, with event references added later), and
``citation_verdicts`` (append-only verifier verdicts).

Revision ID: m3F_0001
Revises: m3_0001
Create Date: 2026-09-25 22:56:05.769369
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

import agenttape.infrastructure.db.base

revision: str = "m3F_0001"
down_revision: str | Sequence[str] | None = "m3_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "findings",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=True),
        sa.Column("node_key", sa.String(length=100), nullable=True),
        sa.Column("origin_event_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("finding_key", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column(
            "details",
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
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["run_attempts.id"],
            name=op.f("fk_findings_attempt_id_run_attempts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_findings_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_findings")),
        sa.UniqueConstraint("origin_event_id", name=op.f("uq_findings_origin_event_id")),
        sa.UniqueConstraint("run_id", "finding_key", name=op.f("uq_findings_run_id_finding_key")),
    )
    with op.batch_alter_table("findings", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_findings_attempt_id"), ["attempt_id"], unique=False)
        batch_op.create_index("ix_findings_run_seq", ["run_id", "sequence"], unique=False)

    op.create_table(
        "citations",
        sa.Column("finding_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("path", sa.Text(), nullable=True),
        sa.Column("inside_boundary", sa.Boolean(), nullable=True),
        sa.Column("line_start", sa.Integer(), nullable=True),
        sa.Column("line_end", sa.Integer(), nullable=True),
        sa.Column("quote", sa.Text(), nullable=True),
        sa.Column("event_ref", sa.String(length=200), nullable=True),
        sa.Column("event_id", sa.String(length=36), nullable=True),
        sa.Column(
            "details",
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
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name=op.f("fk_citations_event_id_events"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["finding_id"],
            ["findings.id"],
            name=op.f("fk_citations_finding_id_findings"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_citations")),
        sa.UniqueConstraint(
            "finding_id", "position", name=op.f("uq_citations_finding_id_position")
        ),
    )
    with op.batch_alter_table("citations", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_citations_event_id"), ["event_id"], unique=False)

    op.create_table(
        "citation_verdicts",
        sa.Column("citation_id", sa.String(length=36), nullable=False),
        sa.Column("verdict", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("method", sa.String(length=32), nullable=False),
        sa.Column("verifier_version", sa.String(length=32), nullable=True),
        sa.Column(
            "evidence",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("rerun_of_id", sa.String(length=36), nullable=True),
        sa.Column(
            "created_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["citation_id"],
            ["citations.id"],
            name=op.f("fk_citation_verdicts_citation_id_citations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_citation_verdicts")),
    )
    with op.batch_alter_table("citation_verdicts", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_citation_verdicts_citation_id"), ["citation_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("citation_verdicts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_citation_verdicts_citation_id"))

    op.drop_table("citation_verdicts")
    with op.batch_alter_table("citations", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_citations_event_id"))

    op.drop_table("citations")
    with op.batch_alter_table("findings", schema=None) as batch_op:
        batch_op.drop_index("ix_findings_run_seq")
        batch_op.drop_index(batch_op.f("ix_findings_attempt_id"))

    op.drop_table("findings")
