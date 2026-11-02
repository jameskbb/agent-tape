"""Redaction at the persistence boundary.

Events, raw logs, artifacts, exports, audit details, and webhook deliveries are redacted
where they are written. This module closes the remaining paths with one chokepoint: a
``before_flush`` hook that redacts free-form JSON and text columns (adapter and workflow
outputs, evaluation evidence, approval payloads, metadata, failure messages) whenever a
row is inserted or those columns change, so no call site can forget.

Run inputs and rendered prompts use :func:`redact_user_text` (credential *patterns* only,
no key-name masking) so secret-reference variables such as ``ci_token: "GITHUB_TOKEN"``
keep their reference names.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session, sessionmaker

from agenttape.infrastructure.db.extra_models.findings import Citation, CitationVerdict, Finding
from agenttape.infrastructure.db.extra_models.observed_sessions import ObservedSession
from agenttape.infrastructure.db.extra_models.verification import (
    CitationReproduction,
    EvidenceRead,
)
from agenttape.infrastructure.db.models import (
    ApprovalRequest,
    Artifact,
    EvaluationResult,
    FileChange,
    Run,
    RunAttempt,
    StepRun,
    TriggerDelivery,
)
from agenttape.security.redaction import DEFAULT_REDACTOR, RedactionReport

#: model -> attribute names redacted on insert/update.
GUARDED: dict[type[Any], tuple[str, ...]] = {
    Run: ("outputs", "notes", "failure_message", "cancel_reason"),
    RunAttempt: ("outputs", "failure_message", "checkpoint"),
    StepRun: ("inputs", "outputs", "state", "failure_message"),
    EvaluationResult: ("evidence", "explanation"),
    ApprovalRequest: ("requested_action", "rationale", "reviewer_note"),
    Artifact: ("metadata_",),
    FileChange: ("metadata_",),
    TriggerDelivery: ("metadata_", "error"),
    Finding: ("text", "details"),
    Citation: ("path", "quote", "event_ref", "details"),
    CitationVerdict: ("reason", "evidence"),
    ObservedSession: ("cwd", "details"),
    EvidenceRead: ("path",),
    CitationReproduction: ("argv", "reason", "output_excerpt", "evidence"),
}


def redact_column(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return DEFAULT_REDACTOR.redact_text(value)
    return DEFAULT_REDACTOR.redact_value(value)


def redact_user_text(value: Any, report: RedactionReport | None = None) -> Any:
    """Pattern-only redaction for user-supplied inputs (keys are variable names)."""
    rep = report if report is not None else RedactionReport()
    if isinstance(value, str):
        return DEFAULT_REDACTOR.redact_text(value, rep)
    if isinstance(value, dict):
        return {k: redact_user_text(v, rep) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_user_text(v, rep) for v in value]
    return value


def _guard(session: Session, _flush_context: Any, _instances: Any) -> None:
    for obj in (*session.new, *session.dirty):
        fields = GUARDED.get(type(obj))
        if not fields:
            continue
        state = inspect(obj)
        for name in fields:
            if not state.pending and not state.attrs[name].history.has_changes():
                continue
            current = getattr(obj, name)
            cleaned = redact_column(current)
            if cleaned != current:
                setattr(obj, name, cleaned)


def install_redaction_guard(session_factory: sessionmaker[Session]) -> None:
    event.listen(session_factory, "before_flush", _guard)
