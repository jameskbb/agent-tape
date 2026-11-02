"""RunService: THE one path for creating, cancelling, retrying, and reading runs.

The API, CLI, scheduler, webhooks, and the workflow engine all call these methods; there
is no second code path. Every created run carries an immutable JSON
snapshot of the configuration that produced it, so later edits to prompts, profiles,
workflows, or projects never rewrite history.
"""

from __future__ import annotations

import platform
import sys
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agenttape import __version__
from agenttape.adapters.base import FindingsCapture
from agenttape.application.context import AppContext
from agenttape.application.events import EventRecorder
from agenttape.application.schemas.catalog import ApprovalOut
from agenttape.application.schemas.common import Page
from agenttape.application.schemas.runs import (
    ArtifactOut,
    AttemptOut,
    EvaluationResultOut,
    EventOut,
    EventPage,
    FileChangeOut,
    RunCreate,
    RunCreateResult,
    RunDetail,
    RunOut,
    StepRunOut,
)
from agenttape.application.services.audit import record_audit
from agenttape.application.services.common import get_or_404, hash_json, like_pattern, paginate
from agenttape.application.services.profiles import AgentProfileService
from agenttape.application.services.prompts import PromptService
from agenttape.application.services.workspaces import default_workspace_id
from agenttape.application.transitions import transition_attempt, transition_run
from agenttape.domain.enums import (
    ApprovalStatus,
    EventType,
    FailureCategory,
    RunKind,
    RunSource,
    RunStatus,
    Severity,
)
from agenttape.domain.errors import (
    ConflictError,
    IdempotencyConflictError,
    NotFoundError,
    ValidationError,
)
from agenttape.domain.events import EventDraft
from agenttape.domain.prompts import (
    RenderMode,
    VariableDefinition,
    render_template,
    template_tokens,
    validate_inputs,
)
from agenttape.findings.review import (
    REVIEW_AUDIT_ACTION,
    REVIEW_RELATION,
    REVIEW_SNAPSHOT_SECTION,
)
from agenttape.git.snapshot import capture_git_state
from agenttape.infrastructure.db.models import (
    ApprovalRequest,
    Artifact,
    EvaluationResult,
    Event,
    FileChange,
    Project,
    Run,
    RunAttempt,
    StepRun,
)
from agenttape.security.persistence import redact_user_text
from agenttape.security.redaction import RedactionReport
from agenttape.worker.hooks import fire_run_terminal_sync

SNAPSHOT_SCHEMA_VERSION = 1

CancelHook = Callable[[AppContext, Session, Run], None]
"""(ctx, session, run) -> None; runs inside the cancellation's transaction."""

_CANCEL_HOOKS: list[CancelHook] = []


def register_cancel_hook(fn: CancelHook) -> None:
    """Be told when a run is cancelled (the workflow engine propagates it to children)."""
    if fn not in _CANCEL_HOOKS:
        _CANCEL_HOOKS.append(fn)


MAX_EVENT_PAGE = 1000

SORTABLE = {
    "created_at": Run.created_at,
    "started_at": Run.started_at,
    "finished_at": Run.finished_at,
    "duration_ms": Run.duration_ms,
    "status": Run.status,
    "title": Run.title,
    "priority": Run.priority,
}


def environment_fingerprint() -> dict[str, Any]:
    """Execution environment recorded in snapshots (used for replay drift warnings)."""
    return {
        "agenttape_version": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(terse=True),
    }


def lock_version(obj: Any, now: datetime) -> None:
    if getattr(obj, "locked_at", None) is None:
        obj.locked_at = now


#: Key in ``Run.options`` holding the fingerprint of the creating request (idempotency).
REQUEST_HASH_KEY = "request_hash"
#: Fields that decide what a run does; presentation fields (title, tags, notes, priority,
#: source_ref) may differ between retries of the same idempotent request.
FINGERPRINT_FIELDS = (
    "kind",
    "project_id",
    "agent_profile_id",
    "agent_profile_version_id",
    "prompt_template_id",
    "prompt_version_id",
    "prompt",
    "inputs",
    "workflow_id",
    "workflow_version_id",
    "parent_run_id",
    "parent_relation",
    "parent_node_key",
    "schedule_id",
    "trigger_id",
    "evaluators",
    "max_attempts",
    "timeout_seconds",
    "idle_timeout_seconds",
    "budget",
)


def _check_reserved_challenge_fields(req: RunCreate, finding_review: dict[str, Any] | None) -> None:
    """Refuse caller-supplied challenge markers (docs/SECURITY.md T27): the challenge
    snapshot section only ever comes from ``create_run``'s internal ``finding_review``
    argument, and the challenge relation/source only accompany it."""
    if REVIEW_SNAPSHOT_SECTION in req.extra_snapshot:
        raise ValidationError(
            f"extra_snapshot cannot set '{REVIEW_SNAPSHOT_SECTION}'; "
            "start a challenge with POST /runs/{run_id}/challenge instead"
        )
    if finding_review is not None:
        return
    if req.source == RunSource.REVIEW:
        raise ValidationError(
            f"source '{RunSource.REVIEW.value}' is reserved for Challenge findings runs"
        )
    if req.parent_relation == REVIEW_RELATION:
        raise ValidationError(
            f"parent_relation '{REVIEW_RELATION}' is reserved for Challenge findings runs"
        )


def request_fingerprint(req: RunCreate) -> str:
    return hash_json(req.model_dump(mode="json", include=set(FINGERPRINT_FIELDS)))


class RunService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.events = EventRecorder(ctx.settings, ctx.clock)

    # ================================================================== create

    def create_run(
        self,
        req: RunCreate,
        *,
        actor: str = "local user",
        finding_review: dict[str, Any] | None = None,
    ) -> RunCreateResult:
        """Create (or return the existing idempotent) run and place it on the queue.

        Reusing an idempotency key with the same request returns the existing run
        (``created=False``); reusing it with a different request raises
        :class:`IdempotencyConflictError` (HTTP 409) instead of silently dropping the new one.

        ``finding_review`` is internal-only: ``FindingReviewService`` passes the challenge's
        snapshot section here, and nothing on ``RunCreate`` can supply it. Without it, a
        request carrying the reserved section in ``extra_snapshot``, the challenge
        ``parent_relation``, or ``RunSource.REVIEW`` is refused (HTTP 422), so a challenge's
        legitimacy is never derived from caller-writable input (docs/SECURITY.md T27).
        """
        _check_reserved_challenge_fields(req, finding_review)
        fingerprint = request_fingerprint(req)
        if req.idempotency_key:
            existing = self._find_idempotent(req.source.value, req.idempotency_key, fingerprint)
            if existing is not None:
                return RunCreateResult(run=existing, created=False)
        try:
            with self.ctx.session() as session:
                run = self._create_in_session(
                    session,
                    req,
                    actor=actor,
                    fingerprint=fingerprint,
                    finding_review=finding_review,
                )
                out = self._run_out(session, run)
        except IntegrityError:
            if req.idempotency_key:
                existing = self._find_idempotent(req.source.value, req.idempotency_key, fingerprint)
                if existing is not None:
                    return RunCreateResult(run=existing, created=False)
            raise
        return RunCreateResult(run=out, created=True)

    def _find_idempotent(
        self, source: str, key: str, fingerprint: str | None = None
    ) -> RunOut | None:
        with self.ctx.session() as session:
            run = session.scalars(
                select(Run).where(Run.source == source, Run.idempotency_key == key)
            ).first()
            if run is None:
                return None
            stored = (run.options or {}).get(REQUEST_HASH_KEY)
            # Runs created before fingerprints existed carry none: treat them as a match.
            if fingerprint is not None and stored is not None and stored != fingerprint:
                raise IdempotencyConflictError(
                    "This idempotency key was already used for a different request",
                    details={"idempotency_key": key, "existing_run_id": run.id},
                )
            return self._run_out(session, run)

    def _create_in_session(
        self,
        session: Session,
        req: RunCreate,
        *,
        actor: str,
        fingerprint: str | None = None,
        finding_review: dict[str, Any] | None = None,
    ) -> Run:
        now = self.ctx.clock.now()
        ws_id = default_workspace_id(session, self.ctx.clock)
        snapshot: dict[str, Any] = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "kind": req.kind,
            "source": req.source.value,
            "source_ref": req.source_ref,
            "captured_at": now.isoformat(),
            "environment": environment_fingerprint(),
        }
        is_demo = bool(req.is_demo)

        # ---------------------------------------------------------------- project
        project: Project | None = None
        if req.project_id:
            project = get_or_404(session, Project, req.project_id, "project")
            if project.archived:
                raise ConflictError("Cannot start runs in an archived project")
            is_demo = is_demo or project.is_demo
            git_state = capture_git_state(project.repo_path)
            snapshot["project"] = {
                "id": project.id,
                "name": project.name,
                "repo_path": project.repo_path,
                "default_branch": project.default_branch,
                "settings": dict(project.settings or {}),
            }
            snapshot["git"] = git_state.as_dict()

        # ---------------------------------------------------------------- agent profile
        profile_config: dict[str, Any] = {}
        adapter_type: str | None = None
        profile_id = profile_version_id = None
        if req.kind == "agent":
            if not req.agent_profile_id:
                raise ValidationError("agent_profile_id is required for agent runs")
            profile, pversion = AgentProfileService(self.ctx).resolve_version(
                session, req.agent_profile_id, req.agent_profile_version_id
            )
            if profile.archived:
                raise ConflictError("Cannot start runs with an archived agent profile")
            if not self.ctx.adapters.has(pversion.adapter_type):
                raise ValidationError(f"Adapter '{pversion.adapter_type}' is not installed")
            is_demo = is_demo or profile.is_demo
            adapter_type = pversion.adapter_type
            profile_config = dict(pversion.config or {})
            profile_id, profile_version_id = profile.id, pversion.id
            snapshot["agent_profile"] = {
                "profile_id": profile.id,
                "name": profile.name,
                "version_id": pversion.id,
                "version": pversion.version,
                "adapter_type": pversion.adapter_type,
                "config": profile_config,
                "config_hash": pversion.config_hash,
            }
            lock_version(pversion, now)
        elif req.agent_profile_id:
            raise ValidationError("agent_profile_id is only valid for agent runs")

        # ---------------------------------------------------------------- prompt
        rendered_text: str | None = None
        inputs = dict(req.inputs)
        template_id = version_id = None
        if req.prompt_template_id:
            template, version = PromptService(self.ctx).resolve_version(
                session, req.prompt_template_id, req.prompt_version_id
            )
            is_demo = is_demo or template.is_demo
            definitions = [VariableDefinition.model_validate(v) for v in version.variables]
            values, issues = validate_inputs(definitions, req.inputs)
            if issues:
                raise ValidationError(
                    "Prompt inputs are invalid",
                    details={"issues": [i.model_dump() for i in issues]},
                )
            rendered = render_template(version.body, definitions, values, mode=RenderMode.REDACT)
            rendered_text = rendered.text
            inputs = values
            template_id, version_id = template.id, version.id
            snapshot["prompt"] = {
                "template_id": template.id,
                "name": template.name,
                "version_id": version.id,
                "version": version.version,
                "body": version.body,
                "variables": list(version.variables or []),
                "content_hash": version.content_hash,
                "rendered": rendered.text,
                "secret_refs": rendered.secret_refs,
            }
            lock_version(version, now)
        elif req.prompt is not None:
            variables, _secrets = template_tokens(req.prompt)
            if variables:
                raise ValidationError(
                    "Inline prompts cannot use {{ variables }}; save a prompt template instead",
                    details={"variables": variables},
                )
            rendered = render_template(req.prompt, [], {}, mode=RenderMode.REDACT)
            rendered_text = rendered.text
            snapshot["prompt"] = {
                "template_id": None,
                "name": None,
                "inline": True,
                "body": req.prompt,
                "variables": [],
                "content_hash": hash_json({"body": req.prompt}),
                "rendered": rendered.text,
                "secret_refs": rendered.secret_refs,
            }
        # Credential-shaped text typed into inputs or an inline prompt is never stored;
        # secret references ({{ secret:NAME }} / secret variables) are the supported path.
        input_report = RedactionReport()
        inputs = redact_user_text(inputs, input_report)
        if rendered_text is not None:
            rendered_text = redact_user_text(rendered_text, input_report)
        prompt_snap = snapshot.get("prompt")
        if prompt_snap:
            prompt_snap["rendered"] = rendered_text
            if prompt_snap.get("inline"):
                prompt_snap["body"] = redact_user_text(prompt_snap["body"], input_report)
        snapshot["inputs"] = inputs

        # ---------------------------------------------------------------- options
        evaluators = (
            [e.model_dump() for e in req.evaluators]
            if req.evaluators is not None
            else list(profile_config.get("evaluators") or [])
        )
        project_settings = dict(project.settings or {}) if project else {}
        options: dict[str, Any] = {
            "evaluators": evaluators,
            "timeout_seconds": req.timeout_seconds
            or profile_config.get("timeout_seconds")
            or project_settings.get("timeout_seconds")
            or self.ctx.settings.default_timeout_seconds,
            "idle_timeout_seconds": req.idle_timeout_seconds
            or profile_config.get("idle_timeout_seconds")
            or project_settings.get("idle_timeout_seconds")
            or self.ctx.settings.default_idle_timeout_seconds,
            "retry": dict(profile_config.get("retry") or {}),
        }
        max_attempts = int(
            req.max_attempts
            or profile_config.get("max_attempts")
            or self.ctx.settings.default_max_attempts
        )
        snapshot["options"] = options
        if req.workflow_version_id or req.workflow_id:
            snapshot.setdefault(
                "workflow", {"workflow_id": req.workflow_id, "version_id": req.workflow_version_id}
            )
        for key, value in req.extra_snapshot.items():
            if key in snapshot and key not in ("workflow",):
                raise ValidationError(f"extra_snapshot cannot override '{key}'")
            snapshot[key] = value
        if finding_review is not None:
            snapshot[REVIEW_SNAPSHOT_SECTION] = dict(finding_review)

        title = req.title or self._default_title(snapshot)
        run = Run(
            workspace_id=ws_id,
            project_id=project.id if project else None,
            title=title[:300],
            kind=req.kind,
            source=req.source.value,
            source_ref=req.source_ref,
            idempotency_key=req.idempotency_key,
            status=RunStatus.QUEUED.value,
            priority=req.priority,
            adapter_type=adapter_type,
            prompt_template_id=template_id,
            prompt_version_id=version_id,
            agent_profile_id=profile_id,
            agent_profile_version_id=profile_version_id,
            workflow_id=req.workflow_id,
            workflow_version_id=req.workflow_version_id,
            schedule_id=req.schedule_id,
            trigger_id=req.trigger_id,
            parent_run_id=req.parent_run_id,
            parent_relation=req.parent_relation,
            parent_node_key=req.parent_node_key,
            inputs=inputs,
            rendered_prompt=rendered_text,
            snapshot=snapshot,
            snapshot_hash=hash_json(snapshot),
            options={**options, REQUEST_HASH_KEY: fingerprint or request_fingerprint(req)},
            tags=req.tags,
            notes=req.notes,
            max_attempts=max_attempts,
            attempt_count=0,
            event_seq=0,
            queue_generation=0,
            queued_at=now,
            budget=req.budget,
            created_by=actor,
            is_demo=is_demo,
            created_at=now,
            updated_at=now,
        )
        session.add(run)
        session.flush()
        self.events.record(
            session,
            run.id,
            EventType.LIFECYCLE,
            "Run queued",
            {
                "status": "queued",
                "source": req.source.value,
                "actor": actor,
                "priority": req.priority,
            },
        )
        if input_report.count:
            self.events.record(
                session,
                run.id,
                EventType.WARNING,
                "Credential-shaped text in the inputs was redacted before storage; "
                "use a secret reference instead",
                {"redaction": input_report.as_metadata()},
                severity=Severity.WARNING,
            )
        if not req.defer_enqueue:
            self.enqueue(session, run)
        record_audit(
            session,
            self.ctx.clock,
            action="run.create",
            resource_type="run",
            resource_id=run.id,
            actor=actor,
            run_id=run.id,
            workspace_id=ws_id,
            details={"source": req.source.value, "kind": req.kind, "title": run.title},
        )
        if finding_review is not None:
            record_audit(
                session,
                self.ctx.clock,
                action=REVIEW_AUDIT_ACTION,
                resource_type="run",
                resource_id=run.id,
                actor=actor,
                run_id=run.id,
                workspace_id=ws_id,
                details={"target_run_id": finding_review.get("target_run_id")},
            )
        return run

    @staticmethod
    def _default_title(snapshot: dict[str, Any]) -> str:
        prompt = snapshot.get("prompt") or {}
        profile = snapshot.get("agent_profile") or {}
        name = prompt.get("name") or ("Inline prompt" if prompt else None)
        who = profile.get("name")
        if name and who:
            return f"{name} with {who}"
        return str(name or who or "Run")

    def enqueue(self, session: Session, run: Run, *, available_at: datetime | None = None) -> str:
        """Place ``run`` on the queue (joins the caller's transaction)."""
        run.queue_generation = (run.queue_generation or 0) + 1
        session.flush()
        return self.ctx.queue.enqueue(
            session,
            dedupe_key=f"run:{run.id}:{run.queue_generation}",
            run_id=run.id,
            priority=run.priority,
            available_at=available_at,
            kind="run",
        )

    # ================================================================== observed

    def create_observed_run(
        self,
        session: Session,
        *,
        project: Project,
        cwd: str,
        source: RunSource,
        source_ref: str,
        start_source: str | None = None,
        model: str | None = None,
    ) -> tuple[Run, RunAttempt]:
        """An ``observed`` run: a Claude Code session AgentTape did not (necessarily)
        start. Joins the caller's transaction (the caller also writes the
        ``observed_sessions`` row in the same transaction) - no queue item, no lease:
        there is no worker to execute this run.
        """
        if project.archived:
            raise ConflictError("Cannot observe sessions in an archived project")
        now = self.ctx.clock.now()
        ws_id = default_workspace_id(session, self.ctx.clock)
        source_value = RunSource(source).value
        snapshot: dict[str, Any] = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "kind": RunKind.OBSERVED.value,
            "source": source_value,
            "source_ref": source_ref,
            "captured_at": now.isoformat(),
            "environment": environment_fingerprint(),
            "project": {
                "id": project.id,
                "name": project.name,
                "repo_path": project.repo_path,
                "default_branch": project.default_branch,
                "settings": dict(project.settings or {}),
            },
            "observed": {
                "agent": "claude-code",
                "cwd": cwd,
                "session_start": start_source,
                "model": model,
            },
        }
        created_by = "claude-code hook" if RunSource(source) == RunSource.HOOK else "agenttape mcp"
        run = Run(
            workspace_id=ws_id,
            project_id=project.id,
            title=f"Observed Claude Code session ({project.name})"[:300],
            kind=RunKind.OBSERVED.value,
            source=source_value,
            source_ref=source_ref,
            status=RunStatus.RUNNING.value,
            adapter_type="claude-code",
            snapshot=snapshot,
            snapshot_hash=hash_json(snapshot),
            options={},
            max_attempts=1,
            attempt_count=1,
            event_seq=0,
            queue_generation=0,
            started_at=now,
            created_by=created_by,
            is_demo=project.is_demo,
            created_at=now,
            updated_at=now,
        )
        session.add(run)
        session.flush()
        attempt = RunAttempt(
            run_id=run.id,
            attempt_number=1,
            status=RunStatus.RUNNING.value,
            workdir=project.repo_path,
            started_at=now,
            created_at=now,
        )
        session.add(attempt)
        session.flush()
        self.events.record(
            session,
            run.id,
            EventType.LIFECYCLE,
            "Observing a Claude Code session AgentTape did not start",
            {
                "phase": "observed_started",
                "status": "running",
                "session_start": start_source,
                "cwd": cwd,
                "model": model,
            },
            attempt_id=attempt.id,
        )
        record_audit(
            session,
            self.ctx.clock,
            action="run.observe",
            resource_type="run",
            resource_id=run.id,
            actor=created_by,
            run_id=run.id,
            workspace_id=ws_id,
            details={"source": source_value, "project_id": project.id},
        )
        return run, attempt

    # ================================================================== cancel / retry

    def cancel_run(
        self, run_id: str, *, reason: str | None = None, actor: str = "local user"
    ) -> RunOut:
        #: Set when this call closed an observed run - fired (below) only after the
        #: transaction below commits, so the terminal hook's own fresh session can see it.
        observed_closed: tuple[str, str] | None = None
        with self.ctx.session() as session:
            run = get_or_404(session, Run, run_id, "run")
            status = RunStatus(run.status)
            now = self.ctx.clock.now()
            message = reason or f"Cancelled by {actor}"
            if status in (RunStatus.CANCELLED, RunStatus.CANCELLING):
                return self._run_out(session, run)
            if run.kind == RunKind.OBSERVED.value and status == RunStatus.RUNNING:
                self._cancel_observed_run(session, run, now, message, actor)
                observed_closed = (run.id, RunStatus.CANCELLED.value)
            elif status in (RunStatus.QUEUED, RunStatus.WAITING_FOR_APPROVAL):
                self.ctx.queue.cancel_for_run(session, run.id)
                self._cancel_pending_approvals(session, run.id, actor=actor)
                self._close_waiting_attempts(session, run.id, now)
                transition_run(
                    session,
                    run,
                    RunStatus.CANCELLED,
                    failure_category=FailureCategory.CANCELLED.value,
                    failure_message=message,
                    cancel_requested_at=now,
                    cancel_reason=message,
                    finished_at=now,
                    duration_ms=self._duration_ms(run, now),
                )
                self.events.append(
                    session,
                    run.id,
                    [
                        _draft(
                            EventType.CANCELLATION,
                            "Run cancelled",
                            {"reason": message, "actor": actor},
                        ),
                        _draft(EventType.LIFECYCLE, "Run cancelled", {"status": "cancelled"}),
                    ],
                )
            elif status in (RunStatus.PREPARING, RunStatus.RUNNING):
                transition_run(
                    session,
                    run,
                    RunStatus.CANCELLING,
                    cancel_requested_at=now,
                    cancel_reason=message,
                )
                self.events.record(
                    session,
                    run.id,
                    EventType.CANCELLATION,
                    "Cancellation requested; stopping the agent process",
                    {"reason": message, "actor": actor},
                    severity=Severity.WARNING,
                )
            else:
                raise ConflictError(
                    f"Run is already {status.value}; nothing to cancel",
                    details={"status": status.value},
                )
            for hook in _CANCEL_HOOKS:
                hook(self.ctx, session, run)
            record_audit(
                session,
                self.ctx.clock,
                action="run.cancel",
                resource_type="run",
                resource_id=run.id,
                actor=actor,
                run_id=run.id,
                workspace_id=run.workspace_id,
                details={"reason": message, "from_status": status.value},
            )
            session.flush()
            out = self._run_out(session, run)
        if observed_closed is not None:
            # Every observed run's terminal transition (SessionEnd, idle sweep, cancel)
            # fires the same run-terminal hooks an ordinary run's completion fires - no
            # second terminal path (automatic verification depends on this).
            fire_run_terminal_sync(self.ctx, *observed_closed)
        return out

    def _cancel_observed_run(
        self, session: Session, run: Run, now: datetime, message: str, actor: str
    ) -> None:
        """Cancelling an observed run has no process to stop: close it directly (RUNNING
        -> CANCELLED at once, never CANCELLING), and stop observing that Claude Code
        session for good - a later hook for it is ignored (``ObservedSessionService``)."""
        attempt = session.scalars(
            select(RunAttempt).where(
                RunAttempt.run_id == run.id, RunAttempt.status == RunStatus.RUNNING.value
            )
        ).first()
        if attempt is not None:
            transition_attempt(
                session,
                attempt,
                RunStatus.CANCELLED,
                failure_category=FailureCategory.CANCELLED.value,
                failure_message=message,
                finished_at=now,
            )
        transition_run(
            session,
            run,
            RunStatus.CANCELLED,
            failure_category=FailureCategory.CANCELLED.value,
            failure_message=message,
            cancel_requested_at=now,
            cancel_reason=message,
            finished_at=now,
            duration_ms=self._duration_ms(run, now),
        )
        self.events.append(
            session,
            run.id,
            [
                _draft(
                    EventType.CANCELLATION,
                    "Observation cancelled; the Claude Code session itself was not touched",
                    {"reason": message, "actor": actor},
                ),
                _draft(EventType.LIFECYCLE, "Run cancelled", {"status": "cancelled"}),
            ],
        )
        # Local import: application.services.observed_sessions imports RunService already.
        from agenttape.application.services.observed_sessions import ObservedSessionService

        ObservedSessionService.close_rows_for_run(session, run.id, reason="cancelled", now=now)

    def _close_waiting_attempts(self, session: Session, run_id: str, now: datetime) -> None:
        for attempt in session.scalars(
            select(RunAttempt).where(
                RunAttempt.run_id == run_id,
                RunAttempt.status == RunStatus.WAITING_FOR_APPROVAL.value,
            )
        ):
            transition_attempt(
                session,
                attempt,
                RunStatus.CANCELLED,
                failure_category=FailureCategory.CANCELLED.value,
                failure_message="Run was cancelled while waiting for approval",
                finished_at=now,
            )

    def _cancel_pending_approvals(self, session: Session, run_id: str, *, actor: str) -> None:
        now = self.ctx.clock.now()
        for approval in session.scalars(
            select(ApprovalRequest).where(
                ApprovalRequest.run_id == run_id,
                ApprovalRequest.status == ApprovalStatus.PENDING.value,
            )
        ):
            approval.status = ApprovalStatus.CANCELLED.value
            approval.resolved_at = now
            approval.reviewer = actor
            approval.reviewer_note = "Run was cancelled"

    def retry_run(
        self, run_id: str, *, reason: str | None = None, actor: str = "local user"
    ) -> RunOut:
        """Retry from failure: the same durable run gets a new attempt."""
        with self.ctx.session() as session:
            run = get_or_404(session, Run, run_id, "run")
            if run.kind == RunKind.OBSERVED.value:
                raise ConflictError(
                    "Observed runs record a Claude Code session AgentTape did not start; "
                    "there is nothing to retry"
                )
            status = RunStatus(run.status)
            if status not in (RunStatus.FAILED, RunStatus.TIMED_OUT, RunStatus.CANCELLED):
                raise ConflictError(
                    f"Only failed, timed-out, or cancelled runs can be retried (run is {status.value})",
                    details={"status": status.value},
                )
            now = self.ctx.clock.now()
            max_attempts = max(run.max_attempts, run.attempt_count + 1)
            transition_run(
                session,
                run,
                RunStatus.QUEUED,
                failure_category=None,
                failure_message=None,
                cancel_requested_at=None,
                cancel_reason=None,
                finished_at=None,
                duration_ms=None,
                queued_at=now,
                max_attempts=max_attempts,
            )
            self.events.append(
                session,
                run.id,
                [
                    _draft(
                        EventType.RETRY,
                        f"Manual retry requested by {actor}",
                        {
                            "reason": reason,
                            "actor": actor,
                            "previous_status": status.value,
                            "manual": True,
                        },
                    ),
                    _draft(EventType.LIFECYCLE, "Run queued", {"status": "queued"}),
                ],
            )
            self.enqueue(session, run)
            record_audit(
                session,
                self.ctx.clock,
                action="run.retry",
                resource_type="run",
                resource_id=run.id,
                actor=actor,
                run_id=run.id,
                workspace_id=run.workspace_id,
                details={"reason": reason, "from_status": status.value},
            )
            session.flush()
            return self._run_out(session, run)

    def update_notes(self, run_id: str, *, notes: str | None, tags: list[str] | None) -> RunOut:
        with self.ctx.session() as session:
            run = get_or_404(session, Run, run_id, "run")
            if notes is not None:
                run.notes = notes
            if tags is not None:
                run.tags = tags
            session.flush()
            return self._run_out(session, run)

    # ================================================================== queries

    def _run_out(self, session: Session, run: Run) -> RunOut:
        out = RunOut.model_validate(run)
        snap = run.snapshot or {}
        out.prompt_name = (snap.get("prompt") or {}).get("name") or (
            "Inline prompt" if snap.get("prompt") else None
        )
        out.agent_profile_name = (snap.get("agent_profile") or {}).get("name")
        out.project_name = (snap.get("project") or {}).get("name")
        out.findings_capture = self._findings_capture(run)
        if run.status == RunStatus.QUEUED.value:
            out.queue_position = self.ctx.queue.position(session, run.id)
        return out

    def _findings_capture(self, run: Run) -> FindingsCapture | None:
        """How ``run``'s own agent records findings: the adapter's answer for the profile
        config the run was snapshotted with, so a later profile edit never rewrites it."""
        if not run.adapter_type or not self.ctx.adapters.has(run.adapter_type):
            return None
        profile = (run.snapshot or {}).get("agent_profile")
        if not isinstance(profile, dict):
            return None
        config = profile.get("config")
        return self.ctx.adapters.get(run.adapter_type).findings_capture_for(
            config if isinstance(config, dict) else {}
        )

    def get_run(self, run_id: str) -> RunOut:
        with self.ctx.session() as session:
            return self._run_out(session, get_or_404(session, Run, run_id, "run"))

    def list_runs(
        self,
        *,
        status: list[str] | None = None,
        source: list[str] | None = None,
        adapter_type: str | None = None,
        project_id: str | None = None,
        prompt_template_id: str | None = None,
        agent_profile_id: str | None = None,
        workflow_id: str | None = None,
        schedule_id: str | None = None,
        parent_run_id: str | None = None,
        failure_category: str | None = None,
        kind: str | None = None,
        search: str | None = None,
        is_demo: bool | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        sort: str = "created_at",
        order: str = "desc",
        limit: int = 50,
        offset: int = 0,
    ) -> Page[RunOut]:
        with self.ctx.session() as session:
            stmt = select(Run)
            if status:
                stmt = stmt.where(Run.status.in_(status))
            if source:
                stmt = stmt.where(Run.source.in_(source))
            if adapter_type:
                stmt = stmt.where(Run.adapter_type == adapter_type)
            if project_id:
                stmt = stmt.where(Run.project_id == project_id)
            if prompt_template_id:
                stmt = stmt.where(Run.prompt_template_id == prompt_template_id)
            if agent_profile_id:
                stmt = stmt.where(Run.agent_profile_id == agent_profile_id)
            if workflow_id:
                stmt = stmt.where(Run.workflow_id == workflow_id)
            if schedule_id:
                stmt = stmt.where(Run.schedule_id == schedule_id)
            if parent_run_id:
                stmt = stmt.where(Run.parent_run_id == parent_run_id)
            if failure_category:
                stmt = stmt.where(Run.failure_category == failure_category)
            if kind:
                stmt = stmt.where(Run.kind == kind)
            if is_demo is not None:
                stmt = stmt.where(Run.is_demo.is_(is_demo))
            if created_after:
                stmt = stmt.where(Run.created_at >= created_after)
            if created_before:
                stmt = stmt.where(Run.created_at < created_before)
            if search:
                pattern = like_pattern(search)
                stmt = stmt.where(
                    or_(
                        func.lower(Run.title).like(pattern, escape="\\"),
                        Run.id.like(f"{search}%"),
                    )
                )
            column = SORTABLE.get(sort)
            if column is None:
                raise ValidationError(
                    f"Cannot sort by '{sort}'", details={"sortable": sorted(SORTABLE)}
                )
            primary = column.asc() if order == "asc" else column.desc()
            if sort in ("started_at", "finished_at", "duration_ms"):
                # Portable NULLS LAST for both SQLite and PostgreSQL.
                stmt = stmt.order_by(column.is_(None), primary, Run.id.desc())
            else:
                stmt = stmt.order_by(primary, Run.id.desc())
            return paginate(
                session,
                stmt,
                limit=limit,
                offset=offset,
                mapper=lambda r: self._run_out(session, r),
            )

    def get_detail(self, run_id: str) -> RunDetail:
        with self.ctx.session() as session:
            run = get_or_404(session, Run, run_id, "run")
            attempts = session.scalars(
                select(RunAttempt)
                .where(RunAttempt.run_id == run_id)
                .order_by(RunAttempt.attempt_number)
            ).all()
            steps = session.scalars(
                select(StepRun)
                .where(StepRun.run_id == run_id)
                .order_by(StepRun.position, StepRun.created_at)
            ).all()
            artifacts = session.scalars(
                select(Artifact).where(Artifact.run_id == run_id).order_by(Artifact.created_at)
            ).all()
            changes = session.scalars(
                select(FileChange).where(FileChange.run_id == run_id).order_by(FileChange.path)
            ).all()
            evaluations = session.scalars(
                select(EvaluationResult)
                .where(EvaluationResult.run_id == run_id)
                .order_by(EvaluationResult.created_at)
            ).all()
            approvals = session.scalars(
                select(ApprovalRequest)
                .where(ApprovalRequest.run_id == run_id)
                .order_by(ApprovalRequest.created_at)
            ).all()
            children = session.scalars(
                select(Run).where(Run.parent_run_id == run_id).order_by(Run.created_at)
            ).all()
            event_count = int(
                session.scalar(
                    select(func.count()).select_from(Event).where(Event.run_id == run_id)
                )
                or 0
            )
            attempt_out = []
            for a in attempts:
                ao = AttemptOut.model_validate(a)
                ao.has_checkpoint = a.checkpoint is not None
                attempt_out.append(ao)
            return RunDetail(
                run=self._run_out(session, run),
                snapshot=dict(run.snapshot or {}),
                inputs=dict(run.inputs or {}),
                rendered_prompt=run.rendered_prompt,
                outputs=run.outputs,
                options=dict(run.options or {}),
                attempts=attempt_out,
                steps=[StepRunOut.model_validate(s) for s in steps],
                artifacts=[ArtifactOut.model_validate(a) for a in artifacts],
                file_changes=[FileChangeOut.model_validate(c) for c in changes],
                evaluations=[EvaluationResultOut.model_validate(e) for e in evaluations],
                approvals=[ApprovalOut.model_validate(a) for a in approvals],
                children=[self._run_out(session, c) for c in children],
                event_count=event_count,
                last_sequence=run.event_seq,
            )

    def list_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 200,
        types: list[str] | None = None,
        severities: list[str] | None = None,
        attempt_id: str | None = None,
        node_key: str | None = None,
        text: str | None = None,
    ) -> EventPage:
        limit = max(1, min(limit, MAX_EVENT_PAGE))
        with self.ctx.session() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise NotFoundError("run", run_id)
            stmt = select(Event).where(Event.run_id == run_id)
            if types:
                stmt = stmt.where(Event.type.in_(types))
            if severities:
                stmt = stmt.where(Event.severity.in_(severities))
            if attempt_id:
                stmt = stmt.where(Event.attempt_id == attempt_id)
            if node_key:
                stmt = stmt.where(Event.node_key == node_key)
            if text:
                stmt = stmt.where(func.lower(Event.summary).like(like_pattern(text), escape="\\"))
            total = int(session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
            rows = session.scalars(
                stmt.where(Event.sequence > after_sequence)
                .order_by(Event.sequence.asc())
                .limit(limit + 1)
            ).all()
            has_more = len(rows) > limit
            rows = rows[:limit]
            return EventPage(
                items=[EventOut.model_validate(r) for r in rows],
                next_after_sequence=rows[-1].sequence if has_more and rows else None,
                last_sequence=run.event_seq,
                total=total,
            )

    def events_after(
        self, session: Session, run_id: str, after_sequence: int, limit: int
    ) -> list[Event]:
        return list(
            session.scalars(
                select(Event)
                .where(Event.run_id == run_id, Event.sequence > after_sequence)
                .order_by(Event.sequence.asc())
                .limit(limit)
            )
        )

    @staticmethod
    def _duration_ms(run: Run, now: datetime) -> int | None:
        start = run.started_at or run.queued_at or run.created_at
        return int((now - start).total_seconds() * 1000) if start else None


def _draft(type_: EventType, summary: str, payload: dict[str, Any], **kwargs: Any) -> EventDraft:
    return EventDraft(type=type_, summary=summary, payload=payload, **kwargs)
