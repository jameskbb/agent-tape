"""Vocabulary, hashing, sanitizing, and source upserts shared by the context modules."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path, PurePath
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agenttape.infrastructure.db.extra_models.context_evidence import (
    ContextEvidenceEvent,
    ContextSource,
)
from agenttape.security.redaction import RedactionReport, Redactor

STAGES = (
    "available",
    "selected",
    "retrieved",
    "injected",
    "referenced",
    "cited",
    "unavailable",
    "unknown",
    "excluded",
    "conflict",
    "stale",
    "gap",
)
AUTHORITIES = ("authoritative", "reference", "snapshot", "generated", "user-provided", "unknown")
RELATIONS = ("supports", "conflicts", "missing")

FEEDBACK_KINDS = (
    "gap",
    "unavailable",
    "stale",
    "conflict",
    "unverified",
    "correction",
    "lesson",
    "doc_suggestion",
)
#: Stages that also open a feedback item so a person can review them.
FEEDBACK_STAGES = ("gap", "unavailable", "stale", "conflict")
CLASSIFICATIONS = ("run-specific", "source-correction", "business-knowledge", "agent-behavior")
DESTINATIONS = (
    "prompt_revision",
    "project_instruction",
    "knowledge_source_update",
    "context_provider_rule",
    "no_promotion",
)
#: What AgentTape proposes for each classification (a person can change it before approval).
PROPOSAL = {
    "run-specific": "no_promotion",
    "source-correction": "knowledge_source_update",
    "business-knowledge": "project_instruction",
    "agent-behavior": "prompt_revision",
}
#: Approved lessons with these destinations are injected into future runs of the project.
#: The others are tracked follow-ups applied outside AgentTape (a knowledge store fix, a
#: provider rule) and are never injected.
INJECTED_DESTINATIONS = ("project_instruction", "prompt_revision")

MAX_ARG_CHARS = 300
MAX_LIST = 50

_URL_QUERY = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^\s?#]+)\?[^\s#]*")
_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/\s@]+@")


def sha256_text(text: str | bytes) -> str:
    data = text.encode() if isinstance(text, str) else text
    return hashlib.sha256(data).hexdigest()


def canonical_hash(value: Any) -> str:
    return sha256_text(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str))


def scope_for(project_id: str | None) -> str:
    return project_id or "global"


def freshness_for(*, stage: str, live: bool | None) -> str:
    if stage == "stale":
        return "stale"
    if live is True:
        return "live"
    if live is False:
        return "snapshot"
    return "unknown"


def _clip(text: str, limit: int = MAX_ARG_CHARS) -> str:
    return text if len(text) <= limit else f"{text[:limit]}…[truncated {len(text) - limit} chars]"


def _cut(text: str | None, limit: int) -> str | None:
    """Fit a value into a ``String(limit)`` column (PostgreSQL rejects longer values)."""
    return text[:limit] if text else None


def sanitize(
    value: Any, report: RedactionReport | None = None, redactor: Redactor | None = None
) -> Any:
    """Redact credentials (patterns and sensitive keys), clip long strings, cap lists."""
    red = redactor or Redactor()
    cleaned = red.redact_value(value, report)

    def clip(v: Any, depth: int) -> Any:
        if depth > 6:
            return "…"
        if isinstance(v, str):
            return _clip(v)
        if isinstance(v, dict):
            return {str(k): clip(x, depth + 1) for k, x in list(v.items())[:MAX_LIST]}
        if isinstance(v, list):
            return [clip(x, depth + 1) for x in v[:MAX_LIST]]
        return v

    return clip(cleaned, 0)


#: The label prefix for a path outside the run's working directory: only the file name is
#: kept (never the host path). Findings, context evidence, and the demo reviewer share it.
OUTSIDE_PREFIX = "<outside>/"


def outside_label(path: str | PurePath) -> str:
    """``<outside>/name``: the display label for a path outside the run's workdir."""
    return OUTSIDE_PREFIX + PurePath(path).name


def safe_locator(raw: str | None, workdir: Path | None = None) -> str | None:
    """A display-safe locator: no credentials, no URL query strings, no absolute paths
    outside the run's working directory (those become ``<outside>/name``)."""
    if raw is None:
        return None
    text = Redactor().redact_text(str(raw))
    text = _USERINFO.sub(r"\1", text)
    text = _URL_QUERY.sub(r"\1", text)
    if text.startswith(("/", "~")) or re.match(r"^[A-Za-z]:[\\/]", text):
        path = Path(text).expanduser()
        if workdir is not None:
            try:
                return path.resolve().relative_to(workdir.resolve()).as_posix()
            except (ValueError, OSError):
                pass
        return outside_label(path)
    return _clip(text, 500)


def upsert_source(
    session: Session,
    *,
    scope: str,
    key: str,
    source_type: str,
    name: str,
    now: datetime,
    provider: str | None = None,
    locator: str | None = None,
    authority: str = "unknown",
    live: bool | None = None,
    version: str | None = None,
    synced_at: str | None = None,
) -> ContextSource:
    """One row per (scope, key); later sightings refresh freshness metadata only."""
    key = key[:300]
    row = session.scalars(
        select(ContextSource).where(ContextSource.scope == scope, ContextSource.source_key == key)
    ).first()
    if authority not in AUTHORITIES:
        authority = "unknown"
    if row is None:
        row = ContextSource(
            scope=scope,
            source_key=key,
            source_type=source_type[:64],
            name=name[:300],
            provider=_cut(provider, 100),
            locator=locator,
            authority=authority,
            live=live,
            version=_cut(version, 200),
            synced_at=_cut(synced_at, 64),
            first_seen_at=now,
            last_seen_at=now,
            details={},
        )
        session.add(row)
        session.flush()
        return row
    row.last_seen_at = now
    if version:
        row.version = _cut(version, 200)
    if synced_at:
        row.synced_at = _cut(synced_at, 64)
    if row.authority == "unknown" and authority != "unknown":
        row.authority = authority
    if row.source_type == "unknown" and source_type != "unknown":
        row.source_type = source_type[:64]
        row.name = name[:300]
    return row


def add_evidence(
    session: Session,
    *,
    run_id: str,
    dedupe_key: str,
    ts: datetime,
    stage: str,
    quality: str,
    source: ContextSource | None,
    attempt_id: str | None = None,
    node_key: str | None = None,
    event_id: str | None = None,
    sequence: int = 0,
    position: int = 0,
    live: bool | None = None,
    version: str | None = None,
    synced_at: str | None = None,
    query: str | None = None,
    tool: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    reason: str | None = None,
    details: dict[str, Any] | None = None,
) -> ContextEvidenceEvent | None:
    """Append one stage record; a repeated ``dedupe_key`` for the run is a no-op."""
    key = dedupe_key[:200]
    exists = session.scalars(
        select(ContextEvidenceEvent.id).where(
            ContextEvidenceEvent.run_id == run_id, ContextEvidenceEvent.dedupe_key == key
        )
    ).first()
    if exists is not None:
        return None
    if stage not in STAGES:
        stage = "unknown"
    live_value = live if live is not None else (source.live if source else None)
    row = ContextEvidenceEvent(
        run_id=run_id,
        attempt_id=attempt_id,
        node_key=node_key,
        event_id=event_id,
        dedupe_key=key,
        sequence=sequence,
        position=position,
        ts=ts,
        stage=stage,
        source_id=source.id if source else None,
        source_key=source.source_key if source else None,
        source_type=source.source_type if source else None,
        source_name=source.name if source else None,
        provider=source.provider if source else None,
        authority=source.authority if source else None,
        live=live_value,
        freshness=freshness_for(stage=stage, live=live_value),
        version=_cut(version, 200),
        synced_at=_cut(synced_at, 64),
        quality=quality,
        query=_clip(query, 1000) if query else None,
        tool=tool,
        result=result,
        reason=_clip(reason, 1000) if reason else None,
        details=details or {},
    )
    session.add(row)
    return row
