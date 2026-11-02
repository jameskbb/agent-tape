"""Concurrency limits enforced when a worker claims a queued run.

Scopes (each optional; a run must fit all that apply):

* global       - Settings > Limits ``max_concurrent_runs``
* per adapter  - Settings > Limits ``per_adapter[<adapter type>]``
* per project  - the project's ``settings.max_concurrent_runs``
* per workflow - the workflow's ``max_concurrency``

"Running" means holding a leased queue item: an executing attempt keeps its lease for its
whole lifetime, while runs waiting for approval or for a retry back-off hold no slot. The
guard runs after the claim commits (see ``infrastructure/queue.py``), so two workers
claiming at the same moment always see each other and can never both be admitted past a
limit. A deferred run gets one ``warning`` event explaining what it is waiting for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, func, select, true
from sqlalchemy.orm import Session

from agenttape.application.events import EventRecorder
from agenttape.config import Settings
from agenttape.domain.enums import EventType, QueueItemStatus, Severity
from agenttape.infrastructure.clock import Clock, SystemClock
from agenttape.infrastructure.db.models import Project, QueueItem, Run, Workflow
from agenttape.limits.settings import load_limits

NOTICE_KEY = "concurrency_wait_notice"


@dataclass(frozen=True)
class Scope:
    kind: str  # global | adapter | project | workflow
    label: str
    limit: int
    clause: ColumnElement[bool]

    def describe(self) -> str:
        return f"{self.label} limit of {self.limit} concurrent run{'s' if self.limit != 1 else ''}"


def scopes_for(session: Session, run: Run) -> list[Scope]:
    limits = load_limits(session)
    scopes: list[Scope] = []
    if limits.max_concurrent_runs:
        scopes.append(Scope("global", "Global", limits.max_concurrent_runs, true()))
    if run.adapter_type and limits.per_adapter.get(run.adapter_type):
        scopes.append(
            Scope(
                "adapter",
                f"Adapter '{run.adapter_type}'",
                int(limits.per_adapter[run.adapter_type]),
                Run.adapter_type == run.adapter_type,
            )
        )
    if run.project_id:
        project = session.get(Project, run.project_id)
        raw = (project.settings or {}).get("max_concurrent_runs") if project else None
        if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 and project is not None:
            scopes.append(
                Scope("project", f"Project '{project.name}'", raw, Run.project_id == project.id)
            )
    if run.workflow_id:
        workflow = session.get(Workflow, run.workflow_id)
        if workflow is not None and workflow.max_concurrency and workflow.max_concurrency > 0:
            scopes.append(
                Scope(
                    "workflow",
                    f"Workflow '{workflow.name}'",
                    workflow.max_concurrency,
                    Run.workflow_id == workflow.id,
                )
            )
    return scopes


def running_in_scope(session: Session, scope: Scope, *, exclude_item: str | None = None) -> int:
    stmt = (
        select(func.count())
        .select_from(QueueItem)
        .join(Run, QueueItem.run_id == Run.id)
        .where(QueueItem.status == QueueItemStatus.LEASED.value, scope.clause)
    )
    if exclude_item:
        stmt = stmt.where(QueueItem.id != exclude_item)
    return int(session.scalar(stmt) or 0)


class ConcurrencyGuard:
    """Claim guard (``register_claim_guard``) enforcing the scopes above."""

    def __init__(self, settings: Settings | None = None, clock: Clock | None = None) -> None:
        # Only the event size caps are read from settings; defaults are fine for a notice.
        self._settings = settings or Settings.model_construct()
        self._clock = clock or SystemClock()

    def __call__(self, session: Session, item: QueueItem) -> str | None:
        if not item.run_id:
            return None
        run = session.get(Run, item.run_id)
        if run is None:
            return None
        for scope in scopes_for(session, run):
            if running_in_scope(session, scope, exclude_item=item.id) >= scope.limit:
                reason = f"Waiting for a free slot: {scope.describe()} reached"
                self._notice(session, item, run, scope, reason)
                return reason
        return None

    def _notice(
        self, session: Session, item: QueueItem, run: Run, scope: Scope, reason: str
    ) -> None:
        payload: dict[str, Any] = dict(item.payload or {})
        if payload.get(NOTICE_KEY) == scope.kind:
            return
        payload[NOTICE_KEY] = scope.kind
        item.payload = payload
        EventRecorder(self._settings, self._clock).record(
            session,
            run.id,
            EventType.WARNING,
            reason,
            {"kind": "concurrency_limit", "scope": scope.kind, "limit": scope.limit},
            severity=Severity.NOTICE,
        )
