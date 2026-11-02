"""Running configured evaluators and persisting EvaluationResult rows."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from sqlalchemy import select

from agenttape.application.context import AppContext
from agenttape.application.events import EventRecorder
from agenttape.application.schemas.runs import EvaluationResultOut
from agenttape.domain.enums import EvaluationStatus, EventType, Severity
from agenttape.evaluations.base import EvaluationContext, EvaluationOutcome, EvaluationSpec
from agenttape.infrastructure.db.models import Artifact, EvaluationResult, Event, FileChange


class EvaluationService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.events = EventRecorder(ctx.settings, ctx.clock)

    def loaders(self, run_id: str) -> dict[str, Any]:
        """Async loaders handed to evaluators via :class:`EvaluationContext`."""

        def _events(types: list[str] | None) -> list[dict[str, Any]]:
            with self.ctx.session() as session:
                stmt = select(Event).where(Event.run_id == run_id)
                if types:
                    stmt = stmt.where(Event.type.in_(types))
                rows = session.scalars(stmt.order_by(Event.sequence)).all()
                return [
                    {
                        "sequence": e.sequence,
                        "type": e.type,
                        "severity": e.severity,
                        "summary": e.summary,
                        "payload": e.payload,
                        "attempt_id": e.attempt_id,
                    }
                    for e in rows
                ]

        def _artifacts() -> list[dict[str, Any]]:
            with self.ctx.session() as session:
                rows = session.scalars(select(Artifact).where(Artifact.run_id == run_id)).all()
                return [
                    {
                        "id": a.id,
                        "name": a.name,
                        "kind": a.kind,
                        "media_type": a.media_type,
                        "size_bytes": a.size_bytes,
                        "sha256": a.sha256,
                        "storage_path": a.storage_path,
                    }
                    for a in rows
                ]

        def _changes() -> list[dict[str, Any]]:
            with self.ctx.session() as session:
                rows = session.scalars(select(FileChange).where(FileChange.run_id == run_id)).all()
                return [
                    {
                        "path": c.path,
                        "change_type": c.change_type,
                        "insertions": c.insertions,
                        "deletions": c.deletions,
                        "is_binary": c.is_binary,
                        "is_protected": c.is_protected,
                    }
                    for c in rows
                ]

        async def load_events(types: list[str] | None = None) -> list[dict[str, Any]]:
            return await asyncio.to_thread(_events, types)

        async def load_artifacts() -> list[dict[str, Any]]:
            return await asyncio.to_thread(_artifacts)

        async def load_file_changes() -> list[dict[str, Any]]:
            return await asyncio.to_thread(_changes)

        return {
            "load_events": load_events,
            "load_artifacts": load_artifacts,
            "load_file_changes": load_file_changes,
        }

    async def evaluate(
        self, spec: EvaluationSpec, ectx: EvaluationContext
    ) -> tuple[EvaluationOutcome, int, str]:
        """Run one evaluator; errors become an ``error`` outcome, never an exception."""
        started = time.monotonic()
        version = "?"
        try:
            evaluator = self.ctx.evaluators.get(spec.type)
            version = evaluator.version
            problems = evaluator.validate_config(spec.config)
            if problems:
                outcome = EvaluationOutcome(
                    status=EvaluationStatus.ERROR,
                    explanation="Evaluator configuration is invalid: " + "; ".join(problems),
                    evidence={"problems": problems},
                )
            else:
                outcome = await evaluator.evaluate(ectx, spec.config)
        except Exception as exc:
            outcome = EvaluationOutcome(
                status=EvaluationStatus.ERROR,
                explanation=f"Evaluator raised {type(exc).__name__}: {exc}",
                evidence={"exception": type(exc).__name__},
            )
        return outcome, int((time.monotonic() - started) * 1000), version

    def persist(
        self,
        *,
        run_id: str,
        attempt_id: str | None,
        step_run_id: str | None,
        spec: EvaluationSpec,
        outcome: EvaluationOutcome,
        duration_ms: int,
        evaluator_version: str,
        node_key: str | None = None,
    ) -> EvaluationResultOut:
        with self.ctx.session() as session:
            result = EvaluationResult(
                run_id=run_id,
                attempt_id=attempt_id,
                step_run_id=step_run_id,
                definition_id=spec.definition_id,
                definition_version_id=spec.definition_version_id,
                name=spec.name or spec.type,
                evaluator_type=spec.type,
                evaluator_version=evaluator_version,
                status=outcome.status.value,
                required=spec.required,
                score=outcome.score,
                evidence=outcome.evidence,
                explanation=outcome.explanation,
                config=spec.config,
                duration_ms=duration_ms,
                created_at=self.ctx.clock.now(),
            )
            session.add(result)
            session.flush()
            severity = (
                Severity.INFO
                if outcome.status in (EvaluationStatus.PASS, EvaluationStatus.SKIPPED)
                else Severity.ERROR
                if spec.required
                and outcome.status in (EvaluationStatus.FAIL, EvaluationStatus.ERROR)
                else Severity.WARNING
            )
            self.events.record(
                session,
                run_id,
                EventType.EVALUATION,
                f"Evaluation {result.name}: {outcome.status.value}",
                {
                    "evaluation_id": result.id,
                    "evaluator_type": spec.type,
                    "status": outcome.status.value,
                    "required": spec.required,
                    "explanation": outcome.explanation,
                    "evidence": outcome.evidence,
                },
                attempt_id=attempt_id,
                step_run_id=step_run_id,
                node_key=node_key,
                severity=severity,
            )
            return EvaluationResultOut.model_validate(result)
