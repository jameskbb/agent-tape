"""M3: findings, their citations, and verifier verdicts.

Three tables, ``FindingsService`` is the only writer for all of them:

* ``findings``          - one row per FINDING event ingested by ``FindingsService``,
  idempotent on ``origin_event_id`` so re-normalizing a run never duplicates a finding.
* ``citations``         - one row per citation a finding declares: a file line range and
  quote, or an event reference. Ordered by ``position`` within
  a finding.
* ``citation_verdicts`` - append-only verifier verdicts for a citation. Rows are never edited; ``rerun_of_id`` mirrors ``evaluation_results.rerun_of_id``
  and links a rerun back to the verdict it supersedes.

Every row here dies with its run (``ondelete="CASCADE"`` all the way down).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from agenttape.infrastructure.db.base import Base, IdMixin, utcnow
from agenttape.infrastructure.db.models import fk


class Finding(IdMixin, Base):
    __tablename__ = "findings"
    __table_args__ = (
        UniqueConstraint("origin_event_id"),
        UniqueConstraint("run_id", "finding_key"),
        Index("ix_findings_run_seq", "run_id", "sequence"),
    )

    run_id: Mapped[str] = fk("runs.id", "CASCADE", nullable=False, index=False)
    attempt_id: Mapped[str | None] = fk("run_attempts.id", "SET NULL")
    node_key: Mapped[str | None] = mapped_column(String(100))
    #: The FINDING event this was derived from; ingestion is idempotent on this column.
    origin_event_id: Mapped[str] = mapped_column(String(36), nullable=False)
    #: The origin event's sequence: recorded order within the run.
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    finding_key: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str | None] = mapped_column(String(32))
    text: Mapped[str | None] = mapped_column(Text)
    #: The origin event's source (which adapter or AgentTape component reported it).
    source: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Citation(IdMixin, Base):
    __tablename__ = "citations"
    __table_args__ = (UniqueConstraint("finding_id", "position"),)

    finding_id: Mapped[str] = fk("findings.id", "CASCADE", nullable=False, index=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    #: "file" or "event".
    kind: Mapped[str] = mapped_column(String(16))
    path: Mapped[str | None] = mapped_column(Text)
    #: True inside the run's working directory, False outside it, None when the working
    #: directory was not known at ingest time.
    inside_boundary: Mapped[bool | None] = mapped_column(Boolean)
    line_start: Mapped[int | None] = mapped_column(Integer)
    line_end: Mapped[int | None] = mapped_column(Integer)
    quote: Mapped[str | None] = mapped_column(Text)
    #: The event reference as the agent gave it.
    event_ref: Mapped[str | None] = mapped_column(String(200))
    #: The AgentTape event it resolved to, in the same run.
    event_id: Mapped[str | None] = fk("events.id", "SET NULL")
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class CitationVerdict(IdMixin, Base):
    __tablename__ = "citation_verdicts"

    citation_id: Mapped[str] = fk("citations.id", "CASCADE", nullable=False, index=True)
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    #: How the verdict was produced (the findings demo seed writes "seeded").
    method: Mapped[str] = mapped_column(String(32), nullable=False)
    verifier_version: Mapped[str | None] = mapped_column(String(32))
    evidence: Mapped[dict[str, Any]] = mapped_column(default=dict)
    #: The previous verdict row for the same citation, when this one reruns it.
    rerun_of_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
