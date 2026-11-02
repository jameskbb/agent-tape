"""M2: File Evidence.

* ``file_evidence_events`` - append-only raw evidence (agent telemetry, tool events,
  command references, filesystem watcher batches, Git reconciliation). ``dedupe_key`` is
  unique per run, so retries and reconciliation never duplicate a row. Noisy directories
  are stored as one ``collapsed`` row per directory per flush (with counts), never one
  row per file.
* ``file_evidence_snapshots`` - the Git/filesystem state at attempt start and end (plus
  the watcher configuration and status for the attempt).
* ``run_file_summaries`` - one row per logical file per run; derived, rebuilt
  deterministically from the two tables above (never edited by hand).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from agenttape.infrastructure.db.base import Base, IdMixin, utcnow
from agenttape.infrastructure.db.models import fk


class FileEvidenceEvent(IdMixin, Base):
    __tablename__ = "file_evidence_events"
    __table_args__ = (
        UniqueConstraint("run_id", "dedupe_key"),
        UniqueConstraint("run_id", "sequence"),
        Index("ix_file_evidence_events_run_id_path", "run_id", "path"),
    )

    run_id: Mapped[str] = fk("runs.id", "CASCADE", nullable=False, index=False)
    attempt_id: Mapped[str | None] = fk("run_attempts.id", "SET NULL")
    node_key: Mapped[str | None] = mapped_column(String(100))
    sequence: Mapped[int] = mapped_column(Integer)
    ts: Mapped[datetime] = mapped_column()
    last_ts: Mapped[datetime | None] = mapped_column()
    #: read | search | reference | create | modify | delete | rename | collapsed
    operation: Mapped[str] = mapped_column(String(16))
    #: Repository-relative path inside the boundary; the declared path when outside.
    path: Mapped[str] = mapped_column(Text)
    previous_path: Mapped[str | None] = mapped_column(Text)
    inside_boundary: Mapped[bool] = mapped_column(Boolean, default=True)
    #: agent_hook | tool_event | filesystem_watcher | git_start | git_end |
    #: command_reference | reconciliation | manual
    source: Mapped[str] = mapped_column(String(24))
    #: observed | reported | inferred | reconciled
    quality: Mapped[str] = mapped_column(String(16))
    adapter: Mapped[str | None] = mapped_column(String(64))
    runtime: Mapped[str | None] = mapped_column(String(32))
    tool: Mapped[str | None] = mapped_column(String(200))
    command: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[str | None] = mapped_column(String(100))
    source_event_id: Mapped[str | None] = mapped_column(String(36))
    before_hash: Mapped[str | None] = mapped_column(String(64))
    after_hash: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    is_binary: Mapped[bool | None] = mapped_column(Boolean)
    #: Coalesced duplicates (debounced watcher events, collapsed directory counts).
    count: Mapped[int] = mapped_column(Integer, default=1)
    protected_patterns: Mapped[list[Any]] = mapped_column(default=list)
    #: The live policy decision for a protected or outside-boundary operation, if any.
    policy: Mapped[dict[str, Any] | None] = mapped_column()
    redacted: Mapped[bool] = mapped_column(Boolean, default=False)
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    dedupe_key: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class FileEvidenceSnapshot(IdMixin, Base):
    __tablename__ = "file_evidence_snapshots"
    __table_args__ = (UniqueConstraint("attempt_id", "kind"),)

    run_id: Mapped[str] = fk("runs.id", "CASCADE", nullable=False)
    attempt_id: Mapped[str] = fk("run_attempts.id", "CASCADE", nullable=False, index=False)
    #: start | end
    kind: Mapped[str] = mapped_column(String(8))
    taken_at: Mapped[datetime] = mapped_column()
    is_repo: Mapped[bool] = mapped_column(Boolean, default=False)
    head: Mapped[str | None] = mapped_column(String(64))
    branch: Mapped[str | None] = mapped_column(String(255))
    dirty: Mapped[bool | None] = mapped_column(Boolean)
    #: {path: [git blob hash | null, size | null, is_binary | null]}; start: every tracked
    #: and untracked (non-ignored) file; end: only paths with evidence or a Git change
    #: (a path is ``[null, null, null]`` when it no longer exists).
    files: Mapped[dict[str, Any]] = mapped_column(default=dict)
    #: watcher config/status, numstat, ignored paths, limits hit.
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)


class RunFileSummary(IdMixin, Base):
    __tablename__ = "run_file_summaries"
    __table_args__ = (UniqueConstraint("run_id", "file_key"),)

    run_id: Mapped[str] = fk("runs.id", "CASCADE", nullable=False)
    #: Logical identity: the final path of the rename chain (or the collapsed directory).
    file_key: Mapped[str] = mapped_column(Text)
    start_path: Mapped[str | None] = mapped_column(Text)
    end_path: Mapped[str | None] = mapped_column(Text)
    rename_chain: Mapped[list[Any]] = mapped_column(default=list)
    first_at: Mapped[datetime | None] = mapped_column()
    last_at: Mapped[datetime | None] = mapped_column()
    operation_counts: Mapped[dict[str, Any]] = mapped_column(default=dict)
    operation_total: Mapped[int] = mapped_column(Integer, default=0)
    first_step: Mapped[str | None] = mapped_column(String(100))
    last_step: Mapped[str | None] = mapped_column(String(100))
    attempts: Mapped[list[Any]] = mapped_column(default=list)
    agents: Mapped[list[Any]] = mapped_column(default=list)
    tools: Mapped[list[Any]] = mapped_column(default=list)
    sources: Mapped[list[Any]] = mapped_column(default=list)
    #: Final user-facing classification (see docs/FILE-EVIDENCE.md).
    classification: Mapped[str] = mapped_column(String(24), index=True)
    classifications: Mapped[list[Any]] = mapped_column(default=list)
    start_hash: Mapped[str | None] = mapped_column(String(64))
    end_hash: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    is_binary: Mapped[bool | None] = mapped_column(Boolean)
    #: added | modified | deleted | renamed | unchanged | unknown | pending
    net_status: Mapped[str] = mapped_column(String(16))
    insertions: Mapped[int | None] = mapped_column(Integer)
    deletions: Mapped[int | None] = mapped_column(Integer)
    net_unchanged: Mapped[bool] = mapped_column(Boolean, default=False)
    is_protected: Mapped[bool] = mapped_column(Boolean, default=False)
    protected_patterns: Mapped[list[Any]] = mapped_column(default=list)
    is_generated: Mapped[bool] = mapped_column(Boolean, default=False)
    is_outside: Mapped[bool] = mapped_column(Boolean, default=False)
    is_collapsed: Mapped[bool] = mapped_column(Boolean, default=False)
    collapsed_count: Mapped[int | None] = mapped_column(Integer)
    #: observed | reconciled_only | telemetry_only | inferred | pending
    completeness: Mapped[str] = mapped_column(String(24))
    explanation: Mapped[str] = mapped_column(Text, default="")
    policy: Mapped[dict[str, Any] | None] = mapped_column()
