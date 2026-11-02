"""Git isolation: where each run executed, and whether AgentTape owns that directory.

One row per run (written by the Git workdir preparer on the first attempt). For an
isolated run the row is the ownership record of the worktree and its
``agenttape/run-<id>`` branch: cleanup only ever touches directories and branches that
have a row here with ``owned=True`` (or orphans inside the AgentTape workspaces dir).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from agenttape.infrastructure.db.base import Base, IdMixin, TimestampMixin
from agenttape.infrastructure.db.models import fk


class RunWorkspace(IdMixin, TimestampMixin, Base):
    __tablename__ = "run_workspaces"
    __table_args__ = (UniqueConstraint("run_id"),)

    run_id: Mapped[str] = fk("runs.id", "CASCADE", nullable=False, index=False)
    project_id: Mapped[str | None] = fk("projects.id", "SET NULL")
    #: The project repository the run started from (canonical path), if any.
    repo_path: Mapped[str | None] = mapped_column(Text)
    #: The directory attempts execute in (the worktree when isolated).
    path: Mapped[str | None] = mapped_column(Text)
    isolated: Mapped[bool] = mapped_column(Boolean, default=False)
    #: True only for worktrees AgentTape created (the only ones it may remove).
    owned: Mapped[bool] = mapped_column(Boolean, default=False)
    branch: Mapped[str | None] = mapped_column(String(255))
    base_commit: Mapped[str | None] = mapped_column(String(64))
    #: active | removed | shared (not isolated) | unavailable (isolation could not start)
    status: Mapped[str] = mapped_column(String(16), index=True)
    adapter_type: Mapped[str | None] = mapped_column(String(64))
    adapter_version: Mapped[str | None] = mapped_column(String(100))
    last_commit_sha: Mapped[str | None] = mapped_column(String(64))
    removed_at: Mapped[datetime | None] = mapped_column()
    #: commits [{sha, message, approval_id, files, created_at}], warnings, cleanup history.
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
