"""M2 fixes: 64-bit file sizes; drop unread file-evidence columns.

* ``size_bytes`` becomes BIGINT on both tables (a file over 2 GiB overflowed on PostgreSQL).
* ``file_evidence_events.canonical_path`` (a root-absolute path nothing read) is dropped.
* ``run_file_summaries.is_ignored`` / ``event_count`` / ``rebuilt_at`` (never read) are
  dropped; summaries are derived and rebuilt from the raw evidence anyway.

Revision ID: m2B_0002
Revises: m2B_0001
Create Date: 2026-09-24 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import agenttape.infrastructure.db.base

revision: str = "m2B_0002"
down_revision: str | Sequence[str] | None = "m2B_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("file_evidence_events", schema=None) as batch_op:
        batch_op.drop_column("canonical_path")
        batch_op.alter_column(
            "size_bytes", existing_type=sa.Integer(), type_=sa.BigInteger(), existing_nullable=True
        )
    with op.batch_alter_table("run_file_summaries", schema=None) as batch_op:
        batch_op.drop_column("is_ignored")
        batch_op.drop_column("event_count")
        batch_op.drop_column("rebuilt_at")
        batch_op.alter_column(
            "size_bytes", existing_type=sa.Integer(), type_=sa.BigInteger(), existing_nullable=True
        )


def downgrade() -> None:
    with op.batch_alter_table("run_file_summaries", schema=None) as batch_op:
        batch_op.alter_column(
            "size_bytes", existing_type=sa.BigInteger(), type_=sa.Integer(), existing_nullable=True
        )
        batch_op.add_column(
            sa.Column(
                "rebuilt_at",
                agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
                nullable=False,
                server_default=sa.func.current_timestamp(),
            )
        )
        batch_op.add_column(
            sa.Column("event_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("is_ignored", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    with op.batch_alter_table("file_evidence_events", schema=None) as batch_op:
        batch_op.alter_column(
            "size_bytes", existing_type=sa.BigInteger(), type_=sa.Integer(), existing_nullable=True
        )
        batch_op.add_column(sa.Column("canonical_path", sa.Text(), nullable=True))
