"""Merge the observed-sessions and verification branches into one head.

``m3C_0001`` (observed sessions) and ``m3V_0002`` (the one-pending-reproduction index at
the end of the verification branch) both descend from ``m3F_0001`` (findings). This
revision reunites them so later revisions chain from a single head. It carries no schema
change; ``upgrade``/``downgrade`` are no-ops.

Revision ID: m3_0002
Revises: m3C_0001, m3V_0002
Create Date: 2026-09-28 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "m3_0002"
down_revision: str | Sequence[str] | None = ("m3C_0001", "m3V_0002")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
