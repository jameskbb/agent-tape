"""Sanitized context section of the run evidence bundle.

``context_export(session, run_id)`` is called by the evidence export. Everything
in it is already stored sanitized (references, hashes, metadata, redacted excerpts only);
result payloads, secret values, and unrestricted absolute paths are never stored, so they
cannot appear here.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agenttape.context_evidence.service import (
    PROVENANCE_STATEMENT,
    evidence_out,
    findings,
    list_feedback,
    list_packs,
    run_summary,
)
from agenttape.infrastructure.db.extra_models.context_evidence import (
    ContextEvidenceEvent,
)

MAX_EXPORT_RECORDS = 10_000


def context_export(session: Session, run_id: str) -> dict[str, Any]:
    summary = run_summary(session, run_id)
    rows = list(
        session.scalars(
            select(ContextEvidenceEvent)
            .where(ContextEvidenceEvent.run_id == run_id)
            .order_by(
                ContextEvidenceEvent.ts,
                ContextEvidenceEvent.sequence,
                ContextEvidenceEvent.position,
                ContextEvidenceEvent.id,
            )
            .limit(MAX_EXPORT_RECORDS + 1)
        )
    )
    return {
        "schema": "agenttape.context-evidence/1",
        "statement": PROVENANCE_STATEMENT,
        "summary": summary,
        "packs": list_packs(session, run_id),
        "evidence": [evidence_out(r) for r in rows[:MAX_EXPORT_RECORDS]],
        "evidence_truncated": len(rows) > MAX_EXPORT_RECORDS,
        "findings": findings(session, run_id),
        "feedback": list_feedback(session, run_id=run_id),
    }
