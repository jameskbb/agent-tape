"""M2: the runtime identity of each attempt (Herdr workspace/pane/agent refs).

One row per attempt that ran through the Herdr adapter (or fell back from it). Written
as soon as the pane exists so a restarted AgentTape can reattach. Only ids, labels, and
flags are stored: never socket paths, native session values, or credentials.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from agenttape.infrastructure.db.base import Base, IdMixin, TimestampMixin
from agenttape.infrastructure.db.models import fk


class RuntimeSession(IdMixin, TimestampMixin, Base):
    __tablename__ = "runtime_sessions"
    __table_args__ = (UniqueConstraint("attempt_id"),)

    run_id: Mapped[str] = fk("runs.id", "CASCADE", nullable=False)
    attempt_id: Mapped[str] = fk("run_attempts.id", "CASCADE", nullable=False, index=False)
    #: herdr | local (a visible pre-start fallback)
    runtime: Mapped[str] = mapped_column(String(32))
    #: "local" or the saved Herdr machine label the configured socket reaches.
    machine: Mapped[str] = mapped_column(String(200), default="local")
    session_name: Mapped[str | None] = mapped_column(String(200))
    herdr_version: Mapped[str | None] = mapped_column(String(64))
    protocol: Mapped[int | None] = mapped_column(Integer)
    workspace_id: Mapped[str | None] = mapped_column(String(64))
    tab_id: Mapped[str | None] = mapped_column(String(64))
    pane_id: Mapped[str | None] = mapped_column(String(64))
    agent_name: Mapped[str | None] = mapped_column(String(64))
    #: claude | codex | demo
    agent_kind: Mapped[str | None] = mapped_column(String(32))
    #: "id" or "path" when Herdr reported a native session reference (the value is not kept).
    native_session_kind: Mapped[str | None] = mapped_column(String(16))
    #: Last observed Herdr lifecycle state: starting|working|blocked|idle|done|unknown
    lifecycle_state: Mapped[str | None] = mapped_column(String(16))
    #: True only for workspaces/tabs AgentTape created (the only ones it may ever close).
    owned_workspace: Mapped[bool] = mapped_column(Boolean, default=False)
    owned_agent: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Bytes of the demo telemetry file already turned into events (reattach resumes here).
    telemetry_offset: Mapped[int] = mapped_column(Integer, default=0)
    last_sync_at: Mapped[datetime | None] = mapped_column()
    #: active | waiting | detached | completed | failed | fallback
    status: Mapped[str] = mapped_column(String(16), index=True)
    failure_category: Mapped[str | None] = mapped_column(String(32))
    #: terminal id, telemetry file name, degraded capabilities, cleanup history.
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
