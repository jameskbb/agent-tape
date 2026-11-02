"""Maps a Claude Code session AgentTape did not necessarily start to a run.

One row per (session, run). The raw Claude Code session id is never stored, only its
``sha256:`` hash (:func:`agenttape.capture.claude_hooks.session_hash`) - consistent with
docs/SECURITY.md T15 (native session values are not kept).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from agenttape.infrastructure.db.base import Base, IdMixin, TimestampMixin
from agenttape.infrastructure.db.models import fk


class ObservedSession(IdMixin, TimestampMixin, Base):
    __tablename__ = "observed_sessions"
    __table_args__ = (
        UniqueConstraint("session_hash", "run_id"),
        Index("ix_observed_sessions_hash_status", "session_hash", "status"),
        Index("ix_observed_sessions_status_seen", "status", "last_seen_at"),
    )

    session_hash: Mapped[str] = mapped_column(String(71), nullable=False, index=False)
    run_id: Mapped[str] = fk("runs.id", "CASCADE", nullable=False, index=False)
    attempt_id: Mapped[str | None] = fk("run_attempts.id", "CASCADE", index=False)
    project_id: Mapped[str | None] = fk("projects.id", "SET NULL", index=False)
    #: observed | attached | hosted
    mode: Mapped[str] = mapped_column(String(16))
    #: hook | mcp
    source: Mapped[str] = mapped_column(String(16))
    agent_kind: Mapped[str] = mapped_column(String(32), default="claude-code")
    cwd: Mapped[str | None] = mapped_column(Text)
    #: active | closed
    status: Mapped[str] = mapped_column(String(16))
    first_seen_at: Mapped[datetime] = mapped_column(nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column()
    #: session_end | idle_window | cancelled | run_ended
    close_reason: Mapped[str | None] = mapped_column(String(32))
    mcp_instance: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
