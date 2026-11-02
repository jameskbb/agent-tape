"""M2: context evidence, context packs, provenance, and feedback.

Five tables:

* ``context_sources``         - one row per distinct source per scope (project id or
  ``global``), so repeated retrievals across runs point at the same source.
* ``context_packs``           - the immutable, versioned, content-hashed pack AgentTape
  assembled for one attempt. The ordered ``items`` (ContextPackItem) and ``excluded``
  lists are JSON because a pack is written once and never edited.
* ``context_evidence_events`` - append-only stage records (available, selected,
  retrieved, injected, referenced, cited, unavailable, unknown, excluded, conflict, stale,
  gap). Idempotent on ``(run_id, dedupe_key)``; the key derives from the source event id.
* ``evidence_links``          - finding/recommendation/decision/... <-> source links with
  a relation (supports/conflicts/missing) and an origin (agent-declared, tool-derived,
  deterministic, manually-confirmed).
* ``context_feedback``        - ContextGap and ContextFeedback in one table: gaps, stale
  and conflicting sources, unverified inferences, user corrections, and candidate lessons,
  with the reviewed learning-loop state (classification, proposed destination, approval,
  lesson version).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from agenttape.infrastructure.db.base import Base, IdMixin, utcnow
from agenttape.infrastructure.db.models import fk


class ContextSource(IdMixin, Base):
    __tablename__ = "context_sources"
    __table_args__ = (UniqueConstraint("scope", "source_key"),)

    #: Project id, or ``global`` for runs without a project.
    scope: Mapped[str] = mapped_column(String(64))
    #: Stable id within the scope (``file:src/app.py``, ``mcp:dw/run_query``, agent ids).
    source_key: Mapped[str] = mapped_column(String(300))
    source_type: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(300))
    provider: Mapped[str | None] = mapped_column(String(100))
    #: Safe display locator (repo-relative path, server/tool); never credentials.
    locator: Mapped[str | None] = mapped_column(Text)
    #: authoritative | reference | snapshot | generated | user-provided | unknown
    authority: Mapped[str] = mapped_column(String(32), default="unknown")
    live: Mapped[bool | None] = mapped_column(Boolean)
    version: Mapped[str | None] = mapped_column(String(200))
    synced_at: Mapped[str | None] = mapped_column(String(64))
    sensitivity: Mapped[str] = mapped_column(String(32), default="internal")
    retention: Mapped[str] = mapped_column(String(64), default="references-and-hashes")
    first_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)


class ContextPack(IdMixin, Base):
    __tablename__ = "context_packs"
    __table_args__ = (UniqueConstraint("attempt_id"), UniqueConstraint("run_id", "version"))

    run_id: Mapped[str] = fk("runs.id", "CASCADE", nullable=False)
    attempt_id: Mapped[str] = fk("run_attempts.id", "CASCADE", nullable=False, index=False)
    node_key: Mapped[str | None] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(Integer)
    policy: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    #: sha256 over the canonical JSON of items + excluded + injected text. Timestamps and
    #: per-run identifiers (trigger/schedule ids, idempotency key, creator, feedback ids)
    #: are left out, so two runs handed the same context share the hash.
    content_hash: Mapped[str] = mapped_column(String(64))
    #: NULL means unavailable: no tokenizer or adapter count exists for this pack.
    token_estimate: Mapped[int | None] = mapped_column(Integer)
    token_method: Mapped[str | None] = mapped_column(String(100))
    items: Mapped[list[Any]] = mapped_column(default=list)
    excluded: Mapped[list[Any]] = mapped_column(default=list)
    redaction: Mapped[dict[str, Any]] = mapped_column(default=dict)
    #: The exact text AgentTape appended to the prompt (approved lessons), if any.
    injected_text: Mapped[str | None] = mapped_column(Text)


class ContextEvidenceEvent(IdMixin, Base):
    __tablename__ = "context_evidence_events"
    __table_args__ = (
        UniqueConstraint("run_id", "dedupe_key"),
        Index("ix_context_evidence_events_run_seq", "run_id", "sequence"),
    )

    run_id: Mapped[str] = fk("runs.id", "CASCADE", nullable=False, index=False)
    attempt_id: Mapped[str | None] = fk("run_attempts.id", "CASCADE")
    node_key: Mapped[str | None] = mapped_column(String(100))
    #: The ``events`` row this was derived from (NULL for context-pack records).
    event_id: Mapped[str | None] = mapped_column(String(36))
    dedupe_key: Mapped[str] = mapped_column(String(200))
    #: The source event's sequence (pack records use 0 and ``position``).
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    position: Mapped[int] = mapped_column(Integer, default=0)
    ts: Mapped[datetime] = mapped_column()
    stage: Mapped[str] = mapped_column(String(32), index=True)
    source_id: Mapped[str | None] = fk("context_sources.id", "SET NULL")
    source_key: Mapped[str | None] = mapped_column(String(300))
    source_type: Mapped[str | None] = mapped_column(String(64))
    source_name: Mapped[str | None] = mapped_column(String(300))
    provider: Mapped[str | None] = mapped_column(String(100))
    authority: Mapped[str | None] = mapped_column(String(32))
    live: Mapped[bool | None] = mapped_column(Boolean)
    #: live | snapshot | stale | unknown
    freshness: Mapped[str] = mapped_column(String(16), default="unknown")
    version: Mapped[str | None] = mapped_column(String(200))
    synced_at: Mapped[str | None] = mapped_column(String(64))
    #: tool-observed | agent-declared | assembled | user-reported
    quality: Mapped[str] = mapped_column(String(32))
    query: Mapped[str | None] = mapped_column(Text)
    #: {"server", "name", "args" (sanitized), "status", "started_at", "ended_at"}
    tool: Mapped[dict[str, Any] | None] = mapped_column()
    #: {"hash", "bytes", "rows", "schema", "truncated", "media_type"}
    result: Mapped[dict[str, Any] | None] = mapped_column()
    reason: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)


class EvidenceLink(IdMixin, Base):
    __tablename__ = "evidence_links"
    __table_args__ = (UniqueConstraint("run_id", "subject_key", "source_key", "relation"),)

    run_id: Mapped[str] = fk("runs.id", "CASCADE", nullable=False)
    #: finding | claim | recommendation | decision | evaluation | artifact | file_change | draft
    subject_type: Mapped[str] = mapped_column(String(32))
    subject_key: Mapped[str] = mapped_column(String(200))
    subject_text: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[str | None] = fk("context_sources.id", "SET NULL")
    source_key: Mapped[str] = mapped_column(String(300))
    #: supports | conflicts | missing
    relation: Mapped[str] = mapped_column(String(16))
    #: agent-declared | tool-derived | deterministic | manually-confirmed
    origin: Mapped[str] = mapped_column(String(32))
    event_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ContextFeedback(IdMixin, Base):
    __tablename__ = "context_feedback"
    __table_args__ = (
        UniqueConstraint("run_id", "dedupe_key"),
        Index("ix_context_feedback_project_status", "project_id", "status"),
    )

    #: The originating run; feedback always stays tied to it.
    run_id: Mapped[str] = fk("runs.id", "CASCADE", nullable=False)
    project_id: Mapped[str | None] = fk("projects.id", "SET NULL", index=False)
    dedupe_key: Mapped[str] = mapped_column(String(200))
    #: gap | unavailable | stale | conflict | unverified | correction | lesson | doc_suggestion
    kind: Mapped[str] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(Text)
    source_key: Mapped[str | None] = mapped_column(String(300))
    event_id: Mapped[str | None] = mapped_column(String(36))
    #: agent-reported | user | agenttape
    reported_by: Mapped[str] = mapped_column(String(32))
    #: The category the reporter suggested (never applied automatically).
    suggested_classification: Mapped[str | None] = mapped_column(String(32))
    #: pending | approved | rejected
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    #: run-specific | source-correction | business-knowledge | agent-behavior
    classification: Mapped[str | None] = mapped_column(String(32))
    #: prompt_revision | project_instruction | knowledge_source_update |
    #: context_provider_rule | no_promotion
    proposed_destination: Mapped[str | None] = mapped_column(String(32))
    lesson_text: Mapped[str | None] = mapped_column(Text)
    #: Per-project version assigned on approval of an injected lesson.
    lesson_version: Mapped[int | None] = mapped_column(Integer)
    reviewed_by: Mapped[str | None] = mapped_column(String(200))
    reviewed_at: Mapped[datetime | None] = mapped_column()
    review_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)
