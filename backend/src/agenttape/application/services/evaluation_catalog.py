"""Versioned evaluator definitions, suites, ad hoc evaluation, reruns, human scores, and
cross-run comparison.

* A definition names an evaluator type plus a configuration. Every configuration change
  is a new immutable version; the first evaluation that uses a version locks it.
* A suite is a list of definitions (optionally pinned to versions, optionally overriding
  ``required``). Workflow evaluation gates and the API run suites.
* Rerunning only applies to *deterministic* evaluators (their result depends solely on
  recorded data: events, artifacts, file changes, outputs, duration, usage). A rerun is a
  new result row pointing at the original through ``rerun_of_id``.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agenttape.application.context import AppContext
from agenttape.application.schemas.common import Page
from agenttape.application.schemas.evaluation_catalog import (
    ComparisonCell,
    ComparisonRow,
    ComparisonRunSummary,
    EvaluateRunRequest,
    EvaluationComparison,
    EvaluationDefinitionCreate,
    EvaluationDefinitionOut,
    EvaluationDefinitionUpdate,
    EvaluationDefinitionVersionCreate,
    EvaluationDefinitionVersionOut,
    EvaluationRunResponse,
    EvaluationSuiteCreate,
    EvaluationSuiteOut,
    EvaluationSuiteUpdate,
    EvaluatorTypeInfo,
    HumanScoreCreate,
)
from agenttape.application.schemas.runs import EvaluationResultOut
from agenttape.application.services.artifacts import ArtifactService
from agenttape.application.services.audit import record_audit
from agenttape.application.services.common import get_or_404, hash_json, like_pattern, paginate
from agenttape.application.services.evaluations import EvaluationService
from agenttape.application.services.workspaces import default_workspace_id
from agenttape.domain.enums import EvaluationStatus
from agenttape.domain.errors import ConflictError, NotFoundError, ValidationError
from agenttape.evaluations.base import EvaluationContext, EvaluationOutcome, EvaluationSpec
from agenttape.infrastructure.db.models import (
    EvaluationDefinition,
    EvaluationDefinitionVersion,
    EvaluationResult,
    EvaluationSuite,
    Run,
    RunAttempt,
)

MAX_COMPARE_RUNS = 6


def artifact_reader(ctx: AppContext) -> Callable[[str, int], Awaitable[str]]:
    """``extras["read_artifact"]`` for evaluators that read artifact contents."""

    async def read(artifact_id: str, max_bytes: int) -> str:
        return await asyncio.to_thread(ArtifactService(ctx).read_text, artifact_id, max_bytes)

    return read


class EvaluationCatalogService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------------ evaluator types

    def evaluator_types(self) -> list[EvaluatorTypeInfo]:
        out: list[EvaluatorTypeInfo] = []
        for e in self.ctx.evaluators.all():
            reason = None
            availability = getattr(e, "availability", None)
            if callable(availability):
                reason = availability(self.ctx)
            out.append(
                EvaluatorTypeInfo(
                    type=e.type,
                    version=e.version,
                    display_name=e.display_name or e.type,
                    description=e.description,
                    deterministic=e.deterministic,
                    config_schema=e.config_schema(),
                    available=reason is None,
                    unavailable_reason=reason,
                )
            )
        return out

    def _check_config(self, evaluator_type: str, config: dict[str, Any]) -> None:
        if not self.ctx.evaluators.has(evaluator_type):
            raise ValidationError(f"Unknown evaluator type '{evaluator_type}'")
        problems = self.ctx.evaluators.get(evaluator_type).validate_config(config)
        if problems:
            raise ValidationError(
                "The evaluator configuration is invalid", details={"problems": problems}
            )

    # ------------------------------------------------------------------ definitions

    def _def_out(self, session: Session, d: EvaluationDefinition) -> EvaluationDefinitionOut:
        out = EvaluationDefinitionOut.model_validate(d)
        latest = self._latest_version(session, d.id)
        out.latest = EvaluationDefinitionVersionOut.model_validate(latest) if latest else None
        if self.ctx.evaluators.has(d.evaluator_type):
            out.deterministic = self.ctx.evaluators.get(d.evaluator_type).deterministic
        return out

    @staticmethod
    def _latest_version(session: Session, definition_id: str) -> EvaluationDefinitionVersion | None:
        return session.scalars(
            select(EvaluationDefinitionVersion)
            .where(EvaluationDefinitionVersion.definition_id == definition_id)
            .order_by(EvaluationDefinitionVersion.version.desc())
            .limit(1)
        ).first()

    def create_definition(
        self, data: EvaluationDefinitionCreate, *, actor: str = "local user", is_demo: bool = False
    ) -> EvaluationDefinitionOut:
        self._check_config(data.evaluator_type, data.config)
        with self.ctx.session() as session:
            ws_id = default_workspace_id(session, self.ctx.clock)
            if session.scalars(
                select(EvaluationDefinition).where(
                    EvaluationDefinition.workspace_id == ws_id,
                    EvaluationDefinition.name == data.name,
                )
            ).first():
                raise ConflictError(f"An evaluation named '{data.name}' already exists")
            now = self.ctx.clock.now()
            definition = EvaluationDefinition(
                workspace_id=ws_id,
                name=data.name,
                description=data.description,
                evaluator_type=data.evaluator_type,
                latest_version=0,
                is_demo=is_demo,
                created_at=now,
                updated_at=now,
            )
            session.add(definition)
            session.flush()
            self._add_version(session, definition, data.config, data.required)
            record_audit(
                session,
                self.ctx.clock,
                action="evaluation_definition.create",
                resource_type="evaluation_definition",
                resource_id=definition.id,
                actor=actor,
                workspace_id=ws_id,
                details={"name": data.name, "evaluator_type": data.evaluator_type},
            )
            return self._def_out(session, definition)

    def _add_version(
        self,
        session: Session,
        definition: EvaluationDefinition,
        config: dict[str, Any],
        required: bool,
    ) -> EvaluationDefinitionVersion:
        definition.latest_version += 1
        version = EvaluationDefinitionVersion(
            definition_id=definition.id,
            version=definition.latest_version,
            config=config,
            required=required,
            content_hash=hash_json(
                {"type": definition.evaluator_type, "config": config, "required": required}
            ),
            created_at=self.ctx.clock.now(),
        )
        session.add(version)
        definition.updated_at = self.ctx.clock.now()
        session.flush()
        return version

    def create_definition_version(
        self,
        definition_id: str,
        data: EvaluationDefinitionVersionCreate,
        *,
        actor: str = "local user",
    ) -> EvaluationDefinitionVersionOut:
        with self.ctx.session() as session:
            definition = get_or_404(
                session, EvaluationDefinition, definition_id, "evaluation definition"
            )
            self._check_config(definition.evaluator_type, data.config)
            latest = self._latest_version(session, definition.id)
            new_hash = hash_json(
                {
                    "type": definition.evaluator_type,
                    "config": data.config,
                    "required": data.required,
                }
            )
            if latest is not None and latest.content_hash == new_hash:
                raise ConflictError(f"No changes since version {latest.version}")
            version = self._add_version(session, definition, data.config, data.required)
            record_audit(
                session,
                self.ctx.clock,
                action="evaluation_definition.version",
                resource_type="evaluation_definition",
                resource_id=definition.id,
                actor=actor,
                workspace_id=definition.workspace_id,
                details={"version": version.version},
            )
            return EvaluationDefinitionVersionOut.model_validate(version)

    def update_definition(
        self, definition_id: str, data: EvaluationDefinitionUpdate, *, actor: str = "local user"
    ) -> EvaluationDefinitionOut:
        with self.ctx.session() as session:
            definition = get_or_404(
                session, EvaluationDefinition, definition_id, "evaluation definition"
            )
            changes = data.model_dump(exclude_unset=True)
            for key, value in changes.items():
                setattr(definition, key, value)
            definition.updated_at = self.ctx.clock.now()
            record_audit(
                session,
                self.ctx.clock,
                action="evaluation_definition.update",
                resource_type="evaluation_definition",
                resource_id=definition.id,
                actor=actor,
                workspace_id=definition.workspace_id,
                details={"fields": sorted(changes)},
            )
            session.flush()
            return self._def_out(session, definition)

    def get_definition(self, definition_id: str) -> EvaluationDefinitionOut:
        with self.ctx.session() as session:
            return self._def_out(
                session,
                get_or_404(session, EvaluationDefinition, definition_id, "evaluation definition"),
            )

    def list_definitions(
        self,
        *,
        search: str | None = None,
        include_archived: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> Page[EvaluationDefinitionOut]:
        with self.ctx.session() as session:
            stmt = select(EvaluationDefinition)
            if not include_archived:
                stmt = stmt.where(EvaluationDefinition.archived.is_(False))
            if search:
                from sqlalchemy import func

                stmt = stmt.where(
                    func.lower(EvaluationDefinition.name).like(like_pattern(search), escape="\\")
                )
            stmt = stmt.order_by(EvaluationDefinition.name.asc(), EvaluationDefinition.id)
            return paginate(
                session,
                stmt,
                limit=limit,
                offset=offset,
                mapper=lambda d: self._def_out(session, d),
            )

    def list_definition_versions(self, definition_id: str) -> list[EvaluationDefinitionVersionOut]:
        with self.ctx.session() as session:
            get_or_404(session, EvaluationDefinition, definition_id, "evaluation definition")
            rows = session.scalars(
                select(EvaluationDefinitionVersion)
                .where(EvaluationDefinitionVersion.definition_id == definition_id)
                .order_by(EvaluationDefinitionVersion.version.desc())
            ).all()
            return [EvaluationDefinitionVersionOut.model_validate(v) for v in rows]

    # ------------------------------------------------------------------ suites

    def _check_members(self, session: Session, members: list[dict[str, Any]]) -> None:
        for m in members:
            definition = session.get(EvaluationDefinition, m["definition_id"])
            if definition is None:
                raise ValidationError(f"Evaluation definition '{m['definition_id']}' not found")
            if (
                m.get("version") is not None
                and not session.scalars(
                    select(EvaluationDefinitionVersion).where(
                        EvaluationDefinitionVersion.definition_id == definition.id,
                        EvaluationDefinitionVersion.version == m["version"],
                    )
                ).first()
            ):
                raise ValidationError(f"'{definition.name}' has no version {m['version']}")

    def create_suite(
        self, data: EvaluationSuiteCreate, *, actor: str = "local user", is_demo: bool = False
    ) -> EvaluationSuiteOut:
        members = [m.model_dump() for m in data.members]
        with self.ctx.session() as session:
            self._check_members(session, members)
            ws_id = default_workspace_id(session, self.ctx.clock)
            if session.scalars(
                select(EvaluationSuite).where(
                    EvaluationSuite.workspace_id == ws_id, EvaluationSuite.name == data.name
                )
            ).first():
                raise ConflictError(f"A suite named '{data.name}' already exists")
            now = self.ctx.clock.now()
            suite = EvaluationSuite(
                workspace_id=ws_id,
                name=data.name,
                description=data.description,
                members=members,
                is_demo=is_demo,
                created_at=now,
                updated_at=now,
            )
            session.add(suite)
            session.flush()
            record_audit(
                session,
                self.ctx.clock,
                action="evaluation_suite.create",
                resource_type="evaluation_suite",
                resource_id=suite.id,
                actor=actor,
                workspace_id=ws_id,
                details={"name": data.name, "members": len(members)},
            )
            return EvaluationSuiteOut.model_validate(suite)

    def update_suite(
        self, suite_id: str, data: EvaluationSuiteUpdate, *, actor: str = "local user"
    ) -> EvaluationSuiteOut:
        with self.ctx.session() as session:
            suite = get_or_404(session, EvaluationSuite, suite_id, "evaluation suite")
            changes = data.model_dump(exclude_unset=True)
            if "members" in changes and changes["members"] is not None:
                self._check_members(session, changes["members"])
            for key, value in changes.items():
                setattr(suite, key, value)
            suite.updated_at = self.ctx.clock.now()
            record_audit(
                session,
                self.ctx.clock,
                action="evaluation_suite.update",
                resource_type="evaluation_suite",
                resource_id=suite.id,
                actor=actor,
                workspace_id=suite.workspace_id,
                details={"fields": sorted(changes)},
            )
            session.flush()
            return EvaluationSuiteOut.model_validate(suite)

    def list_suites(self, *, limit: int = 50, offset: int = 0) -> Page[EvaluationSuiteOut]:
        with self.ctx.session() as session:
            stmt = select(EvaluationSuite).order_by(EvaluationSuite.name, EvaluationSuite.id)
            return paginate(
                session, stmt, limit=limit, offset=offset, mapper=EvaluationSuiteOut.model_validate
            )

    def get_suite(self, suite_id: str) -> EvaluationSuiteOut:
        with self.ctx.session() as session:
            return EvaluationSuiteOut.model_validate(
                get_or_404(session, EvaluationSuite, suite_id, "evaluation suite")
            )

    # ------------------------------------------------------------------ specs

    def resolve_specs(
        self, definition_ids: list[str], suite_id: str | None = None
    ) -> list[EvaluationSpec]:
        """Turn definitions and a suite into evaluator specs; locks the versions used."""
        wanted: list[tuple[str, int | None, bool | None]] = [
            (d, None, None) for d in definition_ids
        ]
        specs: list[EvaluationSpec] = []
        with self.ctx.session() as session:
            if suite_id:
                suite = get_or_404(session, EvaluationSuite, suite_id, "evaluation suite")
                for m in suite.members or []:
                    wanted.append((m["definition_id"], m.get("version"), m.get("required")))
            now = self.ctx.clock.now()
            for def_id, version_no, required in wanted:
                definition = session.get(EvaluationDefinition, def_id)
                if definition is None:
                    raise NotFoundError("evaluation definition", def_id)
                stmt = select(EvaluationDefinitionVersion).where(
                    EvaluationDefinitionVersion.definition_id == def_id
                )
                if version_no is not None:
                    stmt = stmt.where(EvaluationDefinitionVersion.version == version_no)
                version = session.scalars(
                    stmt.order_by(EvaluationDefinitionVersion.version.desc()).limit(1)
                ).first()
                if version is None:
                    raise NotFoundError("evaluation definition version", f"{def_id}@{version_no}")
                if version.locked_at is None:
                    version.locked_at = now
                specs.append(
                    EvaluationSpec(
                        type=definition.evaluator_type,
                        name=definition.name,
                        config=dict(version.config or {}),
                        required=version.required if required is None else required,
                        definition_id=definition.id,
                        definition_version_id=version.id,
                    )
                )
        return specs

    # ------------------------------------------------------------------ evaluate runs

    def _context(self, run_id: str, service: EvaluationService) -> EvaluationContext:
        with self.ctx.session() as session:
            run = get_or_404(session, Run, run_id, "run")
            attempt = session.scalars(
                select(RunAttempt)
                .where(RunAttempt.run_id == run_id)
                .order_by(RunAttempt.attempt_number.desc())
                .limit(1)
            ).first()
            workdir = Path(attempt.workdir) if attempt and attempt.workdir else None
            return EvaluationContext(
                run_id=run.id,
                attempt_id=attempt.id if attempt else None,
                step_run_id=None,
                exit_code=attempt.exit_code if attempt else None,
                outcome=run.status,
                outputs=dict(run.outputs or {}),
                workdir=workdir if workdir and workdir.is_dir() else None,
                duration_ms=run.duration_ms,
                usage=run.usage,
                extras={
                    "project_settings": dict(
                        ((run.snapshot or {}).get("project") or {}).get("settings") or {}
                    ),
                    "cost": run.cost,
                    "read_artifact": artifact_reader(self.ctx),
                },
                **service.loaders(run.id),
            )

    async def evaluate_run(
        self, run_id: str, request: EvaluateRunRequest, *, actor: str = "local user"
    ) -> EvaluationRunResponse:
        specs = [EvaluationSpec.from_dict(e.model_dump()) for e in request.evaluators]
        specs.extend(
            await asyncio.to_thread(self.resolve_specs, request.definition_ids, request.suite_id)
        )
        if not specs:
            raise ValidationError("Choose at least one evaluator, definition, or suite")
        service = EvaluationService(self.ctx)
        ectx = await asyncio.to_thread(self._context, run_id, service)
        results: list[EvaluationResultOut] = []
        for spec in specs:
            outcome, took, version = await service.evaluate(spec, ectx)
            saved = await asyncio.to_thread(
                functools.partial(
                    service.persist,
                    run_id=run_id,
                    attempt_id=ectx.attempt_id,
                    step_run_id=None,
                    spec=spec,
                    outcome=outcome,
                    duration_ms=took,
                    evaluator_version=version,
                )
            )
            if request.suite_id:
                await asyncio.to_thread(
                    self._set_result_fields, saved.id, suite_id=request.suite_id
                )
            results.append(saved)
        await asyncio.to_thread(self._audit, run_id, "evaluation.run", actor, len(results))
        return EvaluationRunResponse(results=results)

    def _set_result_fields(self, result_id: str, **fields: Any) -> None:
        with self.ctx.session() as session:
            row = session.get(EvaluationResult, result_id)
            if row is not None:
                for key, value in fields.items():
                    setattr(row, key, value)

    def _audit(self, run_id: str, action: str, actor: str, count: int) -> None:
        with self.ctx.session() as session:
            run = session.get(Run, run_id)
            record_audit(
                session,
                self.ctx.clock,
                action=action,
                resource_type="run",
                resource_id=run_id,
                actor=actor,
                run_id=run_id,
                workspace_id=run.workspace_id if run else None,
                details={"results": count},
            )

    async def rerun(
        self, run_id: str, result_ids: list[str], *, actor: str = "local user"
    ) -> EvaluationRunResponse:
        """Rerun deterministic evaluations of ``run_id`` against its recorded data."""

        def _load() -> list[EvaluationResult]:
            with self.ctx.session() as session:
                get_or_404(session, Run, run_id, "run")
                stmt = select(EvaluationResult).where(EvaluationResult.run_id == run_id)
                if result_ids:
                    stmt = stmt.where(EvaluationResult.id.in_(result_ids))
                rows = list(session.scalars(stmt.order_by(EvaluationResult.created_at)))
                session.expunge_all()
                return rows

        rows = await asyncio.to_thread(_load)
        if result_ids and len(rows) != len(set(result_ids)):
            raise NotFoundError("evaluation result", ",".join(result_ids))
        # Only the newest result per name is rerun (older ones were already superseded).
        latest: dict[str, EvaluationResult] = {}
        for row in rows:
            latest[row.name] = row
        service = EvaluationService(self.ctx)
        ectx = await asyncio.to_thread(self._context, run_id, service)
        results: list[EvaluationResultOut] = []
        skipped: list[dict[str, Any]] = []
        for row in latest.values():
            if not self.ctx.evaluators.has(row.evaluator_type):
                skipped.append(
                    {"id": row.id, "name": row.name, "reason": "evaluator not installed"}
                )
                continue
            evaluator = self.ctx.evaluators.get(row.evaluator_type)
            if not evaluator.deterministic:
                skipped.append(
                    {
                        "id": row.id,
                        "name": row.name,
                        "reason": "not deterministic (depends on the live workspace or a person)",
                    }
                )
                continue
            spec = EvaluationSpec(
                type=row.evaluator_type,
                name=row.name,
                config=dict(row.config or {}),
                required=row.required,
                definition_id=row.definition_id,
                definition_version_id=row.definition_version_id,
            )
            outcome, took, version = await service.evaluate(spec, ectx)
            outcome.evidence.setdefault("rerun_of", row.id)
            saved = await asyncio.to_thread(
                functools.partial(
                    service.persist,
                    run_id=run_id,
                    attempt_id=row.attempt_id,
                    step_run_id=row.step_run_id,
                    spec=spec,
                    outcome=outcome,
                    duration_ms=took,
                    evaluator_version=version,
                )
            )
            await asyncio.to_thread(self._set_result_fields, saved.id, rerun_of_id=row.id)
            results.append(saved)
        if not results and not skipped:
            raise ConflictError("This run has no evaluation results to rerun")
        await asyncio.to_thread(self._audit, run_id, "evaluation.rerun", actor, len(results))
        return EvaluationRunResponse(results=results, skipped=skipped)

    def human_score(
        self, run_id: str, data: HumanScoreCreate, *, actor: str = "local user"
    ) -> EvaluationResultOut:
        if data.score > data.max_score:
            raise ValidationError("score cannot exceed max_score")
        if data.verdict is not None:
            status = EvaluationStatus(data.verdict)
        elif data.pass_threshold is not None:
            status = (
                EvaluationStatus.PASS
                if data.score >= data.pass_threshold
                else EvaluationStatus.FAIL
            )
        else:
            status = EvaluationStatus.PASS
        reviewer = data.reviewer if data.reviewer != "local user" else actor
        explanation = f"{reviewer} scored {data.score:g} / {data.max_score:g}" + (
            f": {data.notes}" if data.notes else ""
        )
        spec = EvaluationSpec(
            type="human_score",
            name=data.name,
            config={
                "max_score": data.max_score,
                "pass_threshold": data.pass_threshold,
            },
            required=data.required,
        )
        outcome = EvaluationOutcome(
            status=status,
            explanation=explanation[:4000],
            evidence={
                "reviewer": reviewer,
                "score": data.score,
                "max_score": data.max_score,
                "pass_threshold": data.pass_threshold,
                "verdict": data.verdict,
                "notes": data.notes,
                "source": "human",
            },
            score=data.score / data.max_score,
        )
        with self.ctx.session() as session:
            get_or_404(session, Run, run_id, "run")
        version = (
            self.ctx.evaluators.get("human_score").version
            if self.ctx.evaluators.has("human_score")
            else "1"
        )
        saved = EvaluationService(self.ctx).persist(
            run_id=run_id,
            attempt_id=None,
            step_run_id=None,
            spec=spec,
            outcome=outcome,
            duration_ms=0,
            evaluator_version=version,
        )
        self._audit(run_id, "evaluation.human_score", reviewer, 1)
        return saved

    # ------------------------------------------------------------------ compare

    def compare(self, run_ids: list[str]) -> EvaluationComparison:
        ids = list(dict.fromkeys(run_ids))
        if len(ids) < 2:
            raise ValidationError("Compare needs at least two runs")
        if len(ids) > MAX_COMPARE_RUNS:
            raise ValidationError(f"Compare at most {MAX_COMPARE_RUNS} runs at once")
        with self.ctx.session() as session:
            runs = {rid: get_or_404(session, Run, rid, "run") for rid in ids}
            rows = session.scalars(
                select(EvaluationResult)
                .where(EvaluationResult.run_id.in_(ids))
                .order_by(EvaluationResult.created_at, EvaluationResult.id)
            ).all()
            table: dict[str, dict[str, Any]] = {}
            for r in rows:
                entry = table.setdefault(r.name, {"type": r.evaluator_type, "cells": {}})
                entry["cells"][r.run_id] = ComparisonCell(
                    result_id=r.id,
                    status=r.status,
                    score=r.score,
                    explanation=r.explanation,
                    required=r.required,
                    evaluator_version=r.evaluator_version,
                    duration_ms=r.duration_ms,
                )
            out_rows = []
            for name in sorted(table):
                cells: dict[str, ComparisonCell] = table[name]["cells"]
                signature = {(cells[i].status, cells[i].score) if i in cells else None for i in ids}
                out_rows.append(
                    ComparisonRow(
                        name=name,
                        evaluator_type=table[name]["type"],
                        cells=cells,
                        differs=len(signature) > 1,
                    )
                )
            summaries = []
            for rid in ids:
                counts: dict[str, int] = {s.value: 0 for s in EvaluationStatus}
                required_failed = 0
                for row in out_rows:
                    cell = row.cells.get(rid)
                    if cell is None:
                        continue
                    counts[cell.status] = counts.get(cell.status, 0) + 1
                    if cell.required and cell.status in ("fail", "error"):
                        required_failed += 1
                summaries.append(
                    ComparisonRunSummary(
                        run_id=rid,
                        title=runs[rid].title,
                        status=runs[rid].status,
                        counts=counts,
                        required_failed=required_failed,
                    )
                )
            return EvaluationComparison(run_ids=ids, runs=summaries, rows=out_rows)
