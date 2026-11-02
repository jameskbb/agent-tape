"""ScheduleService: schedule CRUD, preview, run-now, history, and occurrence dispatch.

The scheduler loop (:mod:`agenttape.scheduler.engine`) and the API/CLI share this service.
Runs are only ever created through :meth:`RunService.create_run` with
``source=schedule``, ``schedule_id``, and the idempotency key
``schedule:<schedule_id>:<occurrence iso>``; the occurrence row is inserted first under
``UNIQUE (schedule_id, occurrence_at)``, so an occurrence can never produce two runs.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agenttape.application.context import AppContext
from agenttape.application.events import EventRecorder
from agenttape.application.schemas.common import Page
from agenttape.application.schemas.runs import RunCreate, RunCreateResult
from agenttape.application.schemas.schedules import (
    OccurrenceOut,
    PreviewItem,
    RunNowResult,
    ScheduleCreate,
    ScheduleOut,
    SchedulePreview,
    SchedulePreviewRequest,
    SchedulerStatusOut,
    ScheduleTargets,
    ScheduleTiming,
    ScheduleUpdate,
    TargetOption,
)
from agenttape.application.services.audit import record_audit
from agenttape.application.services.common import get_or_404, paginate
from agenttape.application.services.runs import RunService
from agenttape.application.services.workspaces import default_workspace_id
from agenttape.domain.enums import ACTIVE_STATUSES, EventType, RunSource, RunStatus
from agenttape.domain.errors import (
    AgentTapeError,
    ConflictError,
    IdempotencyConflictError,
    NotFoundError,
    ValidationError,
)
from agenttape.domain.prompts import VariableDefinition, validate_inputs
from agenttape.infrastructure.db.models import (
    AgentProfile,
    Project,
    PromptTemplate,
    PromptVersion,
    Run,
    Schedule,
    ScheduleOccurrence,
    WorkerHeartbeat,
    Workflow,
    WorkflowVersion,
)
from agenttape.scheduler.calc import (
    build_calculator,
    load_zone,
    local_note,
    to_utc,
)
from agenttape.scheduler.policies import (
    ConcurrencyAction,
    OccurrenceStatus,
    decide_concurrency,
)

log = logging.getLogger("agenttape.scheduler")

SCHEDULER_LEASE = "scheduler"
#: Run statuses that count as "this schedule still has a run in flight".
OPEN_RUN_STATUSES = tuple(
    s.value for s in (*ACTIVE_STATUSES, RunStatus.QUEUED, RunStatus.WAITING_FOR_APPROVAL)
)
SORTABLE = {
    "name": Schedule.name,
    "next_run_at": Schedule.next_run_at,
    "last_run_at": Schedule.last_run_at,
    "created_at": Schedule.created_at,
}


def workflow_runs_available() -> bool:
    """True once a ``workflow`` run executor is installed."""
    from agenttape.worker.registry import load_executors

    return "workflow" in load_executors()


def workflow_run_fields(
    session: Session, workflow_id: str, workflow_version_id: str | None
) -> dict[str, Any]:
    """``RunCreate`` fields for a workflow run (core contract: ``kind="workflow"``, the
    workflow version id, and the definition in ``extra_snapshot["workflow"]``)."""
    workflow = session.get(Workflow, workflow_id)
    if workflow is None:
        raise NotFoundError("workflow", workflow_id)
    if workflow_version_id:
        version = session.get(WorkflowVersion, workflow_version_id)
    else:
        version = session.scalars(
            select(WorkflowVersion).where(
                WorkflowVersion.workflow_id == workflow.id,
                WorkflowVersion.version == workflow.latest_version,
            )
        ).first()
    if version is None or version.workflow_id != workflow.id:
        raise ValidationError("The workflow has no saved version to run")
    if not workflow_runs_available():
        raise ValidationError("Workflow runs are not available in this build")
    return {
        "kind": "workflow",
        "workflow_id": workflow.id,
        "workflow_version_id": version.id,
        "extra_snapshot": {
            "workflow": {
                "workflow_id": workflow.id,
                "name": workflow.name,
                "version_id": version.id,
                "version": version.version,
                "definition": dict(version.definition or {}),
            }
        },
    }


def local_iso(value: datetime | None, zone: ZoneInfo) -> str | None:
    return value.astimezone(zone).isoformat() if value is not None else None


def occurrence_key(schedule_id: str, occurrence_at: datetime, *, manual: bool = False) -> str:
    stamp = occurrence_at.isoformat()
    return f"schedule:{schedule_id}:{'manual:' if manual else ''}{stamp}"


class ScheduleService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.events = EventRecorder(ctx.settings, ctx.clock)

    # ================================================================== validation

    def _timing_utc(self, timing: ScheduleTiming) -> dict[str, Any]:
        zone = load_zone(timing.timezone)
        return {
            "kind": timing.kind,
            "timezone": timing.timezone,
            "cron_expression": (timing.cron_expression or "").strip() or None,
            "interval_seconds": timing.interval_seconds,
            "run_at": to_utc(timing.run_at, zone) if timing.run_at else None,
            "start_at": to_utc(timing.start_at, zone) if timing.start_at else None,
            "end_at": to_utc(timing.end_at, zone) if timing.end_at else None,
        }

    @staticmethod
    def _calculator(s: Schedule) -> Any:
        return build_calculator(
            kind=s.kind,
            timezone=s.timezone,
            cron_expression=s.cron_expression,
            interval_seconds=s.interval_seconds,
            run_at=s.run_at,
            start_at=s.start_at,
            end_at=s.end_at,
            anchor=s.created_at,
        )

    def _validate_target(self, session: Session, s: Schedule) -> None:
        if s.target_type == "prompt":
            if not s.prompt_template_id:
                raise ValidationError("Choose a saved prompt for this schedule")
            if not s.agent_profile_id:
                raise ValidationError("Choose an agent profile to run the prompt with")
            template = get_or_404(session, PromptTemplate, s.prompt_template_id, "prompt")
            if template.archived:
                raise ConflictError("That prompt is archived")
            version = self._prompt_version(session, template, s.prompt_version_id)
            profile = get_or_404(session, AgentProfile, s.agent_profile_id, "agent profile")
            if profile.archived:
                raise ConflictError("That agent profile is archived")
            if version is not None:
                definitions = [VariableDefinition.model_validate(v) for v in version.variables]
                _values, issues = validate_inputs(definitions, dict(s.inputs or {}))
                if issues:
                    raise ValidationError(
                        "Schedule inputs do not match the prompt's variables",
                        details={"issues": [i.model_dump() for i in issues]},
                    )
            s.workflow_id = None
            s.workflow_version_id = None
        elif s.target_type == "workflow":
            if not s.workflow_id:
                raise ValidationError("Choose a workflow for this schedule")
            workflow = get_or_404(session, Workflow, s.workflow_id, "workflow")
            if workflow.archived:
                raise ConflictError("That workflow is archived")
            if s.workflow_version_id:
                wv = get_or_404(session, WorkflowVersion, s.workflow_version_id, "workflow version")
                if wv.workflow_id != workflow.id:
                    raise ValidationError("That version belongs to a different workflow")
            if not workflow_runs_available():
                raise ValidationError(
                    "Workflow runs are not available in this build yet; schedule a prompt instead"
                )
            s.prompt_template_id = None
            s.prompt_version_id = None
            s.agent_profile_id = None
        else:
            raise ValidationError(f"Unknown target type '{s.target_type}'")
        if s.project_id:
            project = get_or_404(session, Project, s.project_id, "project")
            if project.archived:
                raise ConflictError("That project is archived")

    @staticmethod
    def _prompt_version(
        session: Session, template: PromptTemplate, version_id: str | None
    ) -> PromptVersion | None:
        if version_id:
            version = get_or_404(session, PromptVersion, version_id, "prompt version")
            if version.template_id != template.id:
                raise ValidationError("That prompt version belongs to a different prompt")
            return version
        return session.scalars(
            select(PromptVersion).where(
                PromptVersion.template_id == template.id,
                PromptVersion.version == template.latest_version,
            )
        ).first()

    def _recompute_next(self, s: Schedule, now: datetime) -> None:
        calc = self._calculator(s)
        s.next_run_at = calc.next_after(now)

    # ================================================================== CRUD

    def create(
        self, data: ScheduleCreate, *, actor: str = "local user", is_demo: bool = False
    ) -> ScheduleOut:
        timing = self._timing_utc(data)
        now = self.ctx.clock.now()
        with self.ctx.session() as session:
            ws_id = default_workspace_id(session, self.ctx.clock)
            if session.scalars(
                select(Schedule.id).where(
                    Schedule.workspace_id == ws_id, Schedule.name == data.name
                )
            ).first():
                raise ConflictError(f"A schedule named '{data.name}' already exists")
            s = Schedule(
                workspace_id=ws_id,
                name=data.name,
                description=data.description,
                target_type=data.target_type,
                project_id=data.project_id,
                prompt_template_id=data.prompt_template_id,
                prompt_version_id=data.prompt_version_id,
                agent_profile_id=data.agent_profile_id,
                workflow_id=data.workflow_id,
                workflow_version_id=data.workflow_version_id,
                inputs=dict(data.inputs),
                enabled=data.enabled,
                misfire_policy=data.misfire_policy,
                concurrency_policy=data.concurrency_policy,
                max_queued=data.max_queued,
                priority=data.priority,
                is_demo=is_demo,
                created_at=now,
                updated_at=now,
                **timing,
            )
            if s.kind == "one_time" and s.run_at is not None and s.run_at <= now:
                raise ValidationError("The one-time run is in the past; pick a future time")
            self._validate_target(session, s)
            if not s.project_id:
                s.project_id = self._default_project(session, s)
            self._recompute_next(s, now)
            session.add(s)
            session.flush()
            record_audit(
                session,
                self.ctx.clock,
                action="schedule.create",
                resource_type="schedule",
                resource_id=s.id,
                actor=actor,
                workspace_id=ws_id,
                details={
                    "name": s.name,
                    "kind": s.kind,
                    "timezone": s.timezone,
                    "enabled": s.enabled,
                },
            )
            return self._out(session, s)

    @staticmethod
    def _default_project(session: Session, s: Schedule) -> str | None:
        if s.prompt_template_id:
            t = session.get(PromptTemplate, s.prompt_template_id)
            if t is not None and t.project_id:
                return t.project_id
        if s.agent_profile_id:
            p = session.get(AgentProfile, s.agent_profile_id)
            if p is not None and p.project_id:
                return p.project_id
        if s.workflow_id:
            w = session.get(Workflow, s.workflow_id)
            if w is not None and w.project_id:
                return w.project_id
        return None

    def update(
        self, schedule_id: str, data: ScheduleUpdate, *, actor: str = "local user"
    ) -> ScheduleOut:
        now = self.ctx.clock.now()
        with self.ctx.session() as session:
            s = get_or_404(session, Schedule, schedule_id, "schedule")
            changes = data.model_dump(exclude_unset=True)
            clear_start = changes.pop("clear_start_at", False)
            clear_end = changes.pop("clear_end_at", False)
            timing_fields = {
                "kind",
                "timezone",
                "cron_expression",
                "interval_seconds",
                "run_at",
                "start_at",
                "end_at",
            }
            timing_changed = bool(timing_fields & changes.keys()) or clear_start or clear_end
            if "name" in changes and changes["name"] != s.name:
                clash = session.scalars(
                    select(Schedule.id).where(
                        Schedule.workspace_id == s.workspace_id,
                        Schedule.name == changes["name"],
                        Schedule.id != s.id,
                    )
                ).first()
                if clash:
                    raise ConflictError(f"A schedule named '{changes['name']}' already exists")
            if timing_changed:
                timing = ScheduleTiming(
                    kind=changes.get("kind", s.kind),
                    timezone=changes.get("timezone", s.timezone),
                    cron_expression=changes.get("cron_expression", s.cron_expression),
                    interval_seconds=changes.get("interval_seconds", s.interval_seconds),
                    run_at=changes.get("run_at", s.run_at),
                    start_at=None if clear_start else changes.get("start_at", s.start_at),
                    end_at=None if clear_end else changes.get("end_at", s.end_at),
                )
                for key, value in self._timing_utc(timing).items():
                    setattr(s, key, value)
            for key in (
                "name",
                "description",
                "target_type",
                "project_id",
                "prompt_template_id",
                "prompt_version_id",
                "agent_profile_id",
                "workflow_id",
                "workflow_version_id",
                "inputs",
                "enabled",
                "misfire_policy",
                "concurrency_policy",
                "max_queued",
                "priority",
            ):
                if key in changes:
                    setattr(s, key, changes[key])
            self._validate_target(session, s)
            if timing_changed or "enabled" in changes:
                self._recompute_next(s, now)
            if changes.get("enabled") is False:
                self._skip_queued(session, s, "Schedule was paused")
            s.updated_at = now
            record_audit(
                session,
                self.ctx.clock,
                action="schedule.update",
                resource_type="schedule",
                resource_id=s.id,
                actor=actor,
                workspace_id=s.workspace_id,
                details={"fields": sorted(changes.keys())},
            )
            session.flush()
            return self._out(session, s)

    def set_enabled(
        self, schedule_id: str, enabled: bool, *, actor: str = "local user"
    ) -> ScheduleOut:
        with self.ctx.session() as session:
            s = get_or_404(session, Schedule, schedule_id, "schedule")
            now = self.ctx.clock.now()
            if s.enabled != enabled:
                s.enabled = enabled
                s.updated_at = now
                if enabled:
                    # Resuming never back-fills the paused period: the next run is computed
                    # from now, so the misfire policy only covers scheduler downtime.
                    self._recompute_next(s, now)
                else:
                    self._skip_queued(session, s, "Schedule was paused")
                record_audit(
                    session,
                    self.ctx.clock,
                    action="schedule.resume" if enabled else "schedule.pause",
                    resource_type="schedule",
                    resource_id=s.id,
                    actor=actor,
                    workspace_id=s.workspace_id,
                    details={"name": s.name},
                )
            session.flush()
            return self._out(session, s)

    def delete(self, schedule_id: str, *, actor: str = "local user") -> None:
        with self.ctx.session() as session:
            s = get_or_404(session, Schedule, schedule_id, "schedule")
            record_audit(
                session,
                self.ctx.clock,
                action="schedule.delete",
                resource_type="schedule",
                resource_id=s.id,
                actor=actor,
                workspace_id=s.workspace_id,
                details={"name": s.name},
            )
            session.delete(s)

    def _skip_queued(self, session: Session, s: Schedule, reason: str) -> None:
        for occ in session.scalars(
            select(ScheduleOccurrence).where(
                ScheduleOccurrence.schedule_id == s.id,
                ScheduleOccurrence.status == OccurrenceStatus.QUEUED.value,
            )
        ):
            occ.status = OccurrenceStatus.SKIPPED.value
            occ.reason = reason

    # ================================================================== queries

    def get(self, schedule_id: str) -> ScheduleOut:
        with self.ctx.session() as session:
            return self._out(session, get_or_404(session, Schedule, schedule_id, "schedule"))

    def list_schedules(
        self,
        *,
        search: str | None = None,
        enabled: bool | None = None,
        project_id: str | None = None,
        sort: str = "name",
        order: str = "asc",
        limit: int = 50,
        offset: int = 0,
    ) -> Page[ScheduleOut]:
        column = SORTABLE.get(sort)
        if column is None:
            raise ValidationError(
                f"Cannot sort by '{sort}'", details={"sortable": sorted(SORTABLE)}
            )
        with self.ctx.session() as session:
            stmt = select(Schedule)
            if search:
                stmt = stmt.where(func.lower(Schedule.name).like(f"%{search.lower()}%"))
            if enabled is not None:
                stmt = stmt.where(Schedule.enabled.is_(enabled))
            if project_id:
                stmt = stmt.where(Schedule.project_id == project_id)
            primary = column.asc() if order == "asc" else column.desc()
            stmt = stmt.order_by(column.is_(None), primary, Schedule.id)
            return paginate(
                session, stmt, limit=limit, offset=offset, mapper=lambda s: self._out(session, s)
            )

    def history(self, schedule_id: str, *, limit: int = 50, offset: int = 0) -> Page[OccurrenceOut]:
        with self.ctx.session() as session:
            s = get_or_404(session, Schedule, schedule_id, "schedule")
            zone = load_zone(s.timezone)
            stmt = (
                select(ScheduleOccurrence)
                .where(ScheduleOccurrence.schedule_id == schedule_id)
                .order_by(ScheduleOccurrence.occurrence_at.desc(), ScheduleOccurrence.id)
            )

            def mapper(o: ScheduleOccurrence) -> OccurrenceOut:
                out = OccurrenceOut.model_validate(o)
                out.occurrence_local = local_iso(o.occurrence_at, zone)
                if o.run_id:
                    out.run_status = session.scalar(select(Run.status).where(Run.id == o.run_id))
                return out

            return paginate(session, stmt, limit=limit, offset=offset, mapper=mapper)

    def _out(self, session: Session, s: Schedule) -> ScheduleOut:
        out = ScheduleOut.model_validate(s)
        try:
            zone = load_zone(s.timezone)
        except ValidationError:
            zone = ZoneInfo("UTC")
        out.next_run_local = local_iso(s.next_run_at if s.enabled else None, zone)
        if not s.enabled:
            out.next_run_at = None
        out.run_at_local = local_iso(s.run_at, zone)
        out.start_at_local = local_iso(s.start_at, zone)
        out.end_at_local = local_iso(s.end_at, zone)
        if s.target_type == "prompt" and s.prompt_template_id:
            out.target_name = session.scalar(
                select(PromptTemplate.name).where(PromptTemplate.id == s.prompt_template_id)
            )
        elif s.workflow_id:
            out.target_name = session.scalar(
                select(Workflow.name).where(Workflow.id == s.workflow_id)
            )
        if s.agent_profile_id:
            out.agent_profile_name = session.scalar(
                select(AgentProfile.name).where(AgentProfile.id == s.agent_profile_id)
            )
        if s.project_id:
            out.project_name = session.scalar(
                select(Project.name).where(Project.id == s.project_id)
            )
        if s.last_run_id:
            out.last_run_status = session.scalar(select(Run.status).where(Run.id == s.last_run_id))
        out.active_runs = self._active_runs(session, s.id)
        out.queued_occurrences = self._queued_count(session, s.id)
        last = session.scalars(
            select(ScheduleOccurrence)
            .where(ScheduleOccurrence.schedule_id == s.id)
            .order_by(ScheduleOccurrence.occurrence_at.desc())
            .limit(1)
        ).first()
        if last is not None:
            out.last_occurrence_status = last.status
            out.last_occurrence_reason = last.reason
        return out

    @staticmethod
    def _active_runs(session: Session, schedule_id: str, exclude_run: str | None = None) -> int:
        stmt = (
            select(func.count())
            .select_from(Run)
            .where(Run.schedule_id == schedule_id, Run.status.in_(OPEN_RUN_STATUSES))
        )
        if exclude_run:
            stmt = stmt.where(Run.id != exclude_run)
        return int(session.scalar(stmt) or 0)

    @staticmethod
    def _queued_count(session: Session, schedule_id: str, exclude: str | None = None) -> int:
        stmt = (
            select(func.count())
            .select_from(ScheduleOccurrence)
            .where(
                ScheduleOccurrence.schedule_id == schedule_id,
                ScheduleOccurrence.status == OccurrenceStatus.QUEUED.value,
            )
        )
        if exclude:
            stmt = stmt.where(ScheduleOccurrence.id != exclude)
        return int(session.scalar(stmt) or 0)

    # ================================================================== preview

    def preview(self, req: SchedulePreviewRequest) -> SchedulePreview:
        try:
            zone = load_zone(req.timezone)
            timing = self._timing_utc(req)
            now = self.ctx.clock.now()
            calc = build_calculator(**timing, anchor=now)
            times = calc.preview(now, req.count)
        except ValidationError as exc:
            return SchedulePreview(valid=False, error=exc.message, timezone=req.timezone)
        items = []
        for t in times:
            local = t.astimezone(zone)
            offset = local.strftime("%z")
            items.append(
                PreviewItem(
                    at=t,
                    local=local.isoformat(),
                    utc_offset=f"UTC{offset[:3]}:{offset[3:]}" if offset else "UTC",
                    note=local_note(t, zone, kind=req.kind),
                )
            )
        return SchedulePreview(
            valid=True,
            timezone=req.timezone,
            occurrences=items,
            finished=len(items) < req.count,
        )

    # ================================================================== targets

    def targets(self) -> ScheduleTargets:
        with self.ctx.session() as session:
            prompts = []
            for t in session.scalars(
                select(PromptTemplate)
                .where(PromptTemplate.archived.is_(False))
                .order_by(PromptTemplate.name)
            ):
                v = self._prompt_version(session, t, None)
                prompts.append(
                    TargetOption(
                        id=t.id,
                        name=t.name,
                        description=t.description,
                        latest_version=t.latest_version,
                        latest_version_id=v.id if v else None,
                        variables=list(v.variables or []) if v else [],
                        project_id=t.project_id,
                        is_demo=t.is_demo,
                    )
                )
            profiles = [
                TargetOption(
                    id=p.id,
                    name=p.name,
                    description=p.description,
                    adapter_type=p.adapter_type,
                    project_id=p.project_id,
                    is_demo=p.is_demo,
                )
                for p in session.scalars(
                    select(AgentProfile)
                    .where(AgentProfile.archived.is_(False))
                    .order_by(AgentProfile.name)
                )
            ]
            projects = [
                TargetOption(id=p.id, name=p.name, description=p.description, is_demo=p.is_demo)
                for p in session.scalars(
                    select(Project).where(Project.archived.is_(False)).order_by(Project.name)
                )
            ]
            workflows = []
            for w in session.scalars(
                select(Workflow).where(Workflow.archived.is_(False)).order_by(Workflow.name)
            ):
                wv = session.scalars(
                    select(WorkflowVersion).where(
                        WorkflowVersion.workflow_id == w.id,
                        WorkflowVersion.version == w.latest_version,
                    )
                ).first()
                workflows.append(
                    TargetOption(
                        id=w.id,
                        name=w.name,
                        description=w.description,
                        latest_version=w.latest_version,
                        latest_version_id=wv.id if wv else None,
                        project_id=w.project_id,
                        is_demo=w.is_demo,
                    )
                )
        return ScheduleTargets(
            prompts=prompts,
            agent_profiles=profiles,
            projects=projects,
            workflows=workflows,
            workflow_runs_available=workflow_runs_available(),
        )

    # ================================================================== scheduler status

    def status(self) -> SchedulerStatusOut:
        holder = self.ctx.leases.holder(SCHEDULER_LEASE)
        now = self.ctx.clock.now()
        with self.ctx.session() as session:
            rows = list(
                session.scalars(
                    select(WorkerHeartbeat)
                    .where(WorkerHeartbeat.kind == "scheduler")
                    .order_by(WorkerHeartbeat.last_heartbeat_at.desc())
                    .limit(10)
                )
            )
        fresh = [
            r
            for r in rows
            if r.status != "stopped" and (now - r.last_heartbeat_at) < timedelta(seconds=60)
        ]
        instances = [
            {
                "id": r.worker_id,
                "hostname": r.hostname,
                "pid": r.pid,
                "status": r.status,
                "role": (r.info or {}).get("role"),
                "last_heartbeat_at": r.last_heartbeat_at,
            }
            for r in rows[:5]
        ]
        last_tick = max((r.last_heartbeat_at for r in fresh), default=None)
        if holder is not None:
            state, message = "running", "The scheduler is running and creating due runs."
        elif fresh:
            state, message = "standby", "A scheduler is starting or waiting for the lease."
        elif rows:
            state = "stopped"
            message = "No scheduler is running. Start `agenttape serve` or `agenttape scheduler`; missed runs follow each schedule's misfire policy."
        else:
            state = "unknown"
            message = "No scheduler has run against this database yet. Start `agenttape serve` or `agenttape scheduler`."
        return SchedulerStatusOut(
            state=state,
            owner_id=holder[0] if holder else None,
            lease_expires_at=holder[1] if holder else None,
            last_tick_at=last_tick,
            instances=instances,
            message=message,
        )

    # ================================================================== occurrences

    def insert_occurrence(
        self,
        session: Session,
        schedule_id: str,
        occurrence_at: datetime,
        status: OccurrenceStatus,
        reason: str | None = None,
    ) -> ScheduleOccurrence | None:
        """Insert under the UNIQUE (schedule_id, occurrence_at) key; None if it exists."""
        occ = ScheduleOccurrence(
            schedule_id=schedule_id,
            occurrence_at=occurrence_at,
            status=status.value,
            reason=reason,
            created_at=self.ctx.clock.now(),
        )
        try:
            with session.begin_nested():
                session.add(occ)
                session.flush()
        except IntegrityError:
            return None
        return occ

    def run_now(
        self, schedule_id: str, *, force: bool = False, actor: str = "local user"
    ) -> RunNowResult:
        """Create a manual occurrence now; the concurrency policy applies unless ``force``."""
        now = self.ctx.clock.now()
        with self.ctx.session() as session:
            s = get_or_404(session, Schedule, schedule_id, "schedule")
            occ = None
            for nudge in range(5):  # two clicks in the same microsecond get distinct instants
                occ = self.insert_occurrence(
                    session,
                    s.id,
                    now + timedelta(microseconds=nudge),
                    OccurrenceStatus.PENDING,
                    reason=f"Run now by {actor}",
                )
                if occ is not None:
                    now = occ.occurrence_at
                    break
            if occ is None:
                raise ConflictError("An occurrence for this instant already exists; try again")
            record_audit(
                session,
                self.ctx.clock,
                action="schedule.run_now",
                resource_type="schedule",
                resource_id=s.id,
                actor=actor,
                workspace_id=s.workspace_id,
                details={"force": force, "occurrence_at": now.isoformat()},
            )
            occ_id = occ.id
        action = self.start_or_hold(occ_id, force=force, manual=True, actor=actor)
        with self.ctx.session() as session:
            occ2 = get_or_404(session, ScheduleOccurrence, occ_id, "occurrence")
            s2 = get_or_404(session, Schedule, schedule_id, "schedule")
            out = OccurrenceOut.model_validate(occ2)
            out.occurrence_local = local_iso(occ2.occurrence_at, load_zone(s2.timezone))
            if occ2.run_id:
                out.run_status = session.scalar(select(Run.status).where(Run.id == occ2.run_id))
        messages = {
            ConcurrencyAction.START: "Run created",
            ConcurrencyAction.REPLACE: "Active run cancelled and replaced",
            ConcurrencyAction.WAIT: "Queued behind the schedule's active run",
            ConcurrencyAction.SKIP: "Skipped: the schedule already has an active run",
        }
        message = messages[action]
        if out.status == OccurrenceStatus.FAILED.value:
            message = f"Run could not be created: {out.reason}"
        return RunNowResult(occurrence=out, run_id=out.run_id, action=action.value, message=message)

    def start_or_hold(
        self,
        occurrence_id: str,
        *,
        force: bool = False,
        manual: bool = False,
        actor: str = "scheduler",
    ) -> ConcurrencyAction:
        """Apply the concurrency policy to a pending occurrence and create its run."""
        replace_ids: list[str] = []
        with self.ctx.session() as session:
            occ = session.get(ScheduleOccurrence, occurrence_id)
            if occ is None or occ.status != OccurrenceStatus.PENDING.value:
                return ConcurrencyAction.SKIP
            s = get_or_404(session, Schedule, occ.schedule_id, "schedule")
            # Crash recovery: the run may already exist (created, then the process died
            # before linking it). Link it rather than letting it count as "active" against
            # its own occurrence.
            keys = [occurrence_key(s.id, occ.occurrence_at, manual=m) for m in (False, True)]
            existing = session.scalars(
                select(Run.id).where(
                    Run.source == RunSource.SCHEDULE.value, Run.idempotency_key.in_(keys)
                )
            ).first()
            if existing is not None:
                occ.status = OccurrenceStatus.CREATED.value
                occ.run_id = existing
                return ConcurrencyAction.START
            active = self._active_runs(session, s.id)
            queued = self._queued_count(session, s.id, exclude=occ.id)
            action = (
                ConcurrencyAction.START
                if force
                else decide_concurrency(
                    s.concurrency_policy,
                    active_runs=active,
                    queued_occurrences=queued,
                    max_queued=s.max_queued,
                )
            )
            if action == ConcurrencyAction.WAIT:
                occ.status = OccurrenceStatus.QUEUED.value
                occ.reason = "Waiting for the schedule's active run to finish (concurrency: queue)"
                return action
            if action == ConcurrencyAction.SKIP:
                occ.status = OccurrenceStatus.SKIPPED.value
                occ.reason = (
                    "Skipped: the queue of waiting occurrences is full"
                    if s.concurrency_policy == "queue"
                    else "Skipped: the schedule already has an active run (concurrency: skip)"
                )
                record_audit(
                    session,
                    self.ctx.clock,
                    action="schedule.occurrence_skipped",
                    resource_type="schedule",
                    resource_id=s.id,
                    actor=actor,
                    workspace_id=s.workspace_id,
                    details={"occurrence_at": occ.occurrence_at.isoformat(), "reason": occ.reason},
                )
                return action
            if action == ConcurrencyAction.REPLACE:
                replace_ids = list(
                    session.scalars(
                        select(Run.id).where(
                            Run.schedule_id == s.id, Run.status.in_(OPEN_RUN_STATUSES)
                        )
                    )
                )
                self._skip_queued(session, s, "Replaced by a newer occurrence (cancel and replace)")
        for run_id in replace_ids:
            try:
                RunService(self.ctx).cancel_run(
                    run_id,
                    reason="Replaced by a newer scheduled occurrence (concurrency: cancel and replace)",
                    actor="scheduler",
                )
            except AgentTapeError as exc:
                log.info("cancel-and-replace could not cancel %s: %s", run_id, exc.message)
            with self.ctx.session() as session:
                session.execute(
                    update(ScheduleOccurrence)
                    .where(ScheduleOccurrence.run_id == run_id)
                    .values(
                        status=OccurrenceStatus.REPLACED.value,
                        reason="Cancelled and replaced by a newer occurrence",
                    )
                )
        self.create_run_for(occurrence_id, manual=manual)
        return action

    def create_run_for(self, occurrence_id: str, *, manual: bool = False) -> str | None:
        """Create (idempotently) the run for an occurrence and link it."""
        with self.ctx.session() as session:
            occ = session.get(ScheduleOccurrence, occurrence_id)
            if occ is None or occ.status not in (
                OccurrenceStatus.PENDING.value,
                OccurrenceStatus.QUEUED.value,
            ):
                return occ.run_id if occ is not None else None
            s = get_or_404(session, Schedule, occ.schedule_id, "schedule")
            zone = load_zone(s.timezone)
            local = occ.occurrence_at.astimezone(zone)
            try:
                req = self._run_request(session, s, occ, local, manual=manual)
            except AgentTapeError as exc:
                self._fail_occurrence(session, s, occ, exc.message)
                return None
            schedule_name, workspace_id = s.name, s.workspace_id
        try:
            result = RunService(self.ctx).create_run(req, actor="scheduler")
        except IdempotencyConflictError as exc:
            # The occurrence already has a run (created before a crash, then the schedule
            # was edited): link that run instead of creating a second one.
            existing_id = str(exc.details.get("existing_run_id") or "")
            result = RunCreateResult(run=RunService(self.ctx).get_run(existing_id), created=False)
        except AgentTapeError as exc:
            with self.ctx.session() as session:
                occ = get_or_404(session, ScheduleOccurrence, occurrence_id, "occurrence")
                s = get_or_404(session, Schedule, occ.schedule_id, "schedule")
                self._fail_occurrence(session, s, occ, exc.message)
            return None
        run_id = result.run.id
        now = self.ctx.clock.now()
        with self.ctx.session() as session:
            occ = get_or_404(session, ScheduleOccurrence, occurrence_id, "occurrence")
            occ.status = OccurrenceStatus.CREATED.value
            occ.run_id = run_id
            if occ.reason and occ.reason.startswith("Waiting"):
                occ.reason = "Started after the previous run finished"
            sched = session.get(Schedule, occ.schedule_id)
            if sched is not None and (sched.last_run_at is None or sched.last_run_at <= now):
                sched.last_run_at = now
                sched.last_run_id = run_id
            if result.created:
                self.events.record(
                    session,
                    run_id,
                    EventType.SCHEDULER,
                    f"Created by schedule '{schedule_name}'"
                    + (" (run now)" if manual else f" for {local.strftime('%Y-%m-%d %H:%M %Z')}"),
                    {
                        "schedule_id": occ.schedule_id,
                        "occurrence_id": occ.id,
                        "occurrence_at": occ.occurrence_at.isoformat(),
                        "occurrence_local": local.isoformat(),
                        "manual": manual,
                    },
                )
                record_audit(
                    session,
                    self.ctx.clock,
                    action="schedule.run_created",
                    resource_type="schedule",
                    resource_id=occ.schedule_id,
                    actor="scheduler",
                    run_id=run_id,
                    workspace_id=workspace_id,
                    details={"occurrence_at": occ.occurrence_at.isoformat(), "manual": manual},
                )
        return run_id

    def _fail_occurrence(
        self, session: Session, s: Schedule, occ: ScheduleOccurrence, message: str
    ) -> None:
        occ.status = OccurrenceStatus.FAILED.value
        occ.reason = message[:2000]
        record_audit(
            session,
            self.ctx.clock,
            action="schedule.occurrence_failed",
            resource_type="schedule",
            resource_id=s.id,
            actor="scheduler",
            workspace_id=s.workspace_id,
            details={"occurrence_at": occ.occurrence_at.isoformat(), "error": message[:500]},
        )

    def _run_request(
        self,
        session: Session,
        s: Schedule,
        occ: ScheduleOccurrence,
        local: datetime,
        *,
        manual: bool,
    ) -> RunCreate:
        key = occurrence_key(s.id, occ.occurrence_at, manual=manual)
        title = f"{s.name} ({'run now' if manual else local.strftime('%Y-%m-%d %H:%M %Z')})"
        common: dict[str, Any] = {
            "title": title[:300],
            "project_id": s.project_id,
            "inputs": dict(s.inputs or {}),
            "priority": s.priority,
            "idempotency_key": key,
            "source": RunSource.SCHEDULE,
            "source_ref": f"schedule:{s.id}:occurrence:{occ.id}",
            "schedule_id": s.id,
            "tags": ["scheduled"],
            "is_demo": True if s.is_demo else None,
        }
        if s.target_type == "workflow":
            if not s.workflow_id:
                raise ValidationError("The schedule's workflow was deleted")
            return RunCreate(
                **workflow_run_fields(session, s.workflow_id, s.workflow_version_id), **common
            )
        if not s.prompt_template_id or not s.agent_profile_id:
            raise ValidationError("The schedule's prompt or agent profile was deleted")
        return RunCreate(
            kind="agent",
            agent_profile_id=s.agent_profile_id,
            prompt_template_id=s.prompt_template_id,
            prompt_version_id=s.prompt_version_id,
            **common,
        )
