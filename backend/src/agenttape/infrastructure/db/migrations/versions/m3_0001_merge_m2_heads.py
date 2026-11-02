"""Merge the three M2 heads into one M3 ancestor.

M2 built three features on their own migration heads: runtime sessions (m2A),
file evidence (m2B), and context evidence (m2C). This revision unites them so M3
branches from a single ancestor. It carries no
schema change; ``upgrade``/``downgrade`` are no-ops.

Revision ID: m3_0001
Revises: m2A_0001, m2B_0002, m2C_0001
Create Date: 2026-09-25 22:55:23.880420
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "m3_0001"
down_revision: str | Sequence[str] | None = ("m2A_0001", "m2B_0002", "m2C_0001")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
