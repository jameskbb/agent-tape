"""Verified findings: retained evidence and citation reproductions.

Three tables:

* ``evidence_blobs``        - one row per (run, sha256) of retained read content. A full,
  unredacted, untruncated read of a file that is a Git blob in the run's repository is
  stored as a reference (``git_blob`` set, ``storage_path`` None, no copy on disk);
  otherwise it is a redacted, size-capped local copy under the run's artifact directory
  (``storage_path`` set, ``git_blob`` None). ``agenttape.findings.retention`` is the only
  writer.
* ``evidence_reads``        - one row per read (per ``tool_result`` event) verification used,
  pointing at the ``EvidenceBlob`` its content lives in. ``agenttape.findings.retention`` is
  the only writer.
* ``citation_reproductions`` - re-running the command or search behind a citation
  (``ReproductionService`` is the only writer). Never touches
  ``citation_verdicts``.

Every row here dies with its run (``ondelete="CASCADE"`` all the way down), so deleting a
run through the existing retention cleanup removes these rows and the retained files with
it, with no changes to retention code: blob files live inside the artifact directory
retention already deletes (``runs/<run_id>/...``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from agenttape.infrastructure.db.base import Base, IdMixin, utcnow
from agenttape.infrastructure.db.models import fk


class EvidenceBlob(IdMixin, Base):
    __tablename__ = "evidence_blobs"
    __table_args__ = (UniqueConstraint("run_id", "sha256"),)

    run_id: Mapped[str] = fk("runs.id", "CASCADE", nullable=False, index=False)
    #: sha256 of the retained bytes: the redacted recorded output for a local copy, the file
    #: text for a Git reference.
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Set only for a Git reference (the blob id in the run's repository).
    git_blob: Mapped[str | None] = mapped_column(String(40))
    #: Artifact-store path of a local copy; ``None`` for a Git reference.
    storage_path: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    redacted: Mapped[bool] = mapped_column(Boolean, default=False)
    media_type: Mapped[str] = mapped_column(String(64), default="text/plain")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class EvidenceRead(IdMixin, Base):
    __tablename__ = "evidence_reads"
    __table_args__ = (
        UniqueConstraint("result_event_id"),
        Index("ix_evidence_reads_run_path", "run_id", "path"),
    )

    run_id: Mapped[str] = fk("runs.id", "CASCADE", nullable=False, index=False)
    result_event_id: Mapped[str] = fk("events.id", "CASCADE", nullable=False)
    call_event_id: Mapped[str | None] = fk("events.id", "SET NULL")
    path: Mapped[str] = mapped_column(Text, nullable=False)
    line_first: Mapped[int | None] = mapped_column(Integer)
    line_last: Mapped[int | None] = mapped_column(Integer)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    redacted: Mapped[bool] = mapped_column(Boolean, default=False)
    #: The Git blob id of the returned lines, only when the read covered the file from line
    #: 1, untruncated (a partial/offset read cannot honestly reconstruct a whole-file hash).
    read_hash: Mapped[str | None] = mapped_column(String(40))
    blob_id: Mapped[str | None] = fk("evidence_blobs.id", "SET NULL")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


#: The pending (queued or running) statuses; at most one such row per citation.
PENDING_REPRODUCTION_WHERE = "status IN ('queued', 'running')"


class CitationReproduction(IdMixin, Base):
    __tablename__ = "citation_reproductions"
    __table_args__ = (
        # At most one pending reproduction per citation, enforced by the database so two
        # concurrent requests cannot both pass the service's own check and both insert.
        Index(
            "ux_citation_reproductions_one_pending",
            "citation_id",
            unique=True,
            sqlite_where=text(PENDING_REPRODUCTION_WHERE),
            postgresql_where=text(PENDING_REPRODUCTION_WHERE),
        ),
    )

    citation_id: Mapped[str] = fk("citations.id", "CASCADE", nullable=False, index=True)
    run_id: Mapped[str] = fk("runs.id", "CASCADE", nullable=False, index=True)
    #: The recorded command/tool event re-run, if any.
    source_event_id: Mapped[str | None] = fk("events.id", "SET NULL")
    #: "automatic" | "user"
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    #: "search" | "command"
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_by: Mapped[str | None] = mapped_column(String(200))
    #: "queued" | "running" | "done"
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    #: "reproduced" | "not_reproduced" | "could_not_run"
    result: Mapped[str | None] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(Text)
    argv: Mapped[list[Any]] = mapped_column(default=list)
    cwd: Mapped[str | None] = mapped_column(Text)
    pinned_commit: Mapped[str | None] = mapped_column(String(64))
    exit_code: Mapped[int | None] = mapped_column(Integer)
    recorded_exit_code: Mapped[int | None] = mapped_column(Integer)
    output_excerpt: Mapped[str | None] = mapped_column(Text)
    output_sha256: Mapped[str | None] = mapped_column(String(64))
    evidence: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()
