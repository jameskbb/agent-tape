"""M3: at most one pending reproduction per citation, enforced by the database.

A partial unique index on ``citation_reproductions.citation_id`` over the pending
statuses (``queued``, ``running``): two concurrent requests for the same citation can no
longer both pass ``ReproductionService.request``'s own check and both insert a queued row.
A finished (``done``) reproduction is outside the index, so a citation keeps its full
history of re-runs.

Revision ID: m3V_0002
Revises: m3V_0001
Create Date: 2026-09-27 21:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "m3V_0002"
down_revision: str | Sequence[str] | None = "m3V_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PENDING = "status IN ('queued', 'running')"


def upgrade() -> None:
    with op.batch_alter_table("citation_reproductions", schema=None) as batch_op:
        batch_op.create_index(
            "ux_citation_reproductions_one_pending",
            ["citation_id"],
            unique=True,
            sqlite_where=sa.text(_PENDING),
            postgresql_where=sa.text(_PENDING),
        )


def downgrade() -> None:
    with op.batch_alter_table("citation_reproductions", schema=None) as batch_op:
        batch_op.drop_index("ux_citation_reproductions_one_pending")
