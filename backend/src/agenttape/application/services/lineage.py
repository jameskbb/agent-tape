"""Replay and fork: new runs derived from an earlier run's immutable snapshot.

Both go through :meth:`RunService.create_run` (the one run engine); this module only
builds the :class:`RunCreate` request from the original snapshot and records how the new
run relates to the old one (``parent_run_id`` plus ``parent_relation`` ``replay`` or
``fork``, and a ``replay``/``fork`` snapshot section).

* **Replay** pins exactly the original prompt version, agent profile version, inputs,
  evaluators, limits, and project.
* **Fork** starts from the same configuration and applies selected changes (prompt
  version of the same template, agent profile and version, variable values).

Before creating either, the current environment is compared with the original: repo
HEAD, dirty state, project settings, adapter version, AgentTape and Python versions.
Differences are returned as drift items, recorded in the new run's snapshot, and emitted
as a warning event so the timeline shows them.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agenttape.application.context import AppContext
from agenttape.application.events import EventRecorder
from agenttape.application.schemas.git import (
    DriftItem,
    ForkOptions,
    ForkRequest,
    LineageCreateResult,
    LineageOut,
    LineageRun,
    ProfileChoice,
    ReplayRequest,
    VersionChoice,
)
from agenttape.application.schemas.runs import EvaluatorConfig, RunCreate
from agenttape.application.services.common import get_or_404, hash_json
from agenttape.application.services.runs import RunService, environment_fingerprint
from agenttape.domain.enums import EventType, RunSource, Severity
from agenttape.domain.errors import ConflictError, ValidationError
from agenttape.git.snapshot import capture_git_state
from agenttape.infrastructure.db.extra_models.git_workspaces import RunWorkspace
from agenttape.infrastructure.db.models import (
    AgentProfile,
    AgentProfileVersion,
    Project,
    PromptVersion,
    Run,
)

REPLAYABLE_KINDS = frozenset({"agent"})


def compute_drift(
    snapshot: dict[str, Any],
    *,
    current_git: dict[str, Any] | None,
    current_project_settings: dict[str, Any] | None,
    project_exists: bool,
    original_adapter_version: str | None,
    current_adapter_version: str | None,
    current_environment: dict[str, Any],
) -> list[DriftItem]:
    """Pure comparison of an original snapshot with the current environment."""
    items: list[DriftItem] = []
    project = snapshot.get("project") or {}
    git = snapshot.get("git") or {}
    if project:
        if not project_exists:
            items.append(
                DriftItem(
                    field="project",
                    label="Project",
                    original=project.get("name"),
                    current=None,
                    message="The original project no longer exists",
                )
            )
        if git.get("is_repo") and current_git is not None:
            if git.get("head") != current_git.get("head"):
                items.append(
                    DriftItem(
                        field="git.head",
                        label="Repository HEAD",
                        original=git.get("head"),
                        current=current_git.get("head"),
                        message="The repository HEAD has moved since the original run",
                    )
                )
            if bool(current_git.get("dirty")) and not git.get("dirty"):
                items.append(
                    DriftItem(
                        field="git.dirty",
                        label="Uncommitted changes",
                        original=git.get("dirty_count", 0),
                        current=current_git.get("dirty_count"),
                        message="The repository now has uncommitted changes",
                    )
                )
            elif git.get("dirty") and current_git.get("dirty_files") != git.get("dirty_files"):
                items.append(
                    DriftItem(
                        field="git.dirty",
                        label="Uncommitted changes",
                        original=git.get("dirty_count", 0),
                        current=current_git.get("dirty_count"),
                        message="The set of uncommitted files differs from the original run",
                    )
                )
        if current_project_settings is not None:
            original_settings = dict(project.get("settings") or {})
            if hash_json(original_settings) != hash_json(current_project_settings):
                changed = sorted(
                    k
                    for k in set(original_settings) | set(current_project_settings)
                    if original_settings.get(k) != current_project_settings.get(k)
                )
                items.append(
                    DriftItem(
                        field="project.settings",
                        label="Project settings",
                        original={k: original_settings.get(k) for k in changed},
                        current={k: current_project_settings.get(k) for k in changed},
                        message="Project settings changed: " + ", ".join(changed),
                    )
                )
    if (
        original_adapter_version
        and current_adapter_version
        and original_adapter_version != current_adapter_version
    ):
        items.append(
            DriftItem(
                field="adapter.version",
                label="Adapter version",
                original=original_adapter_version,
                current=current_adapter_version,
                message="The agent adapter version differs from the original run",
            )
        )
    original_env = snapshot.get("environment") or {}
    for key, label in (
        ("agenttape_version", "AgentTape version"),
        ("python", "Python version"),
        ("platform", "Platform"),
    ):
        if key in original_env and original_env.get(key) != current_environment.get(key):
            items.append(
                DriftItem(
                    field=f"environment.{key}",
                    label=label,
                    original=original_env.get(key),
                    current=current_environment.get(key),
                    message=f"{label} differs from the original run",
                )
            )
    return items


class LineageService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.runs = RunService(ctx)
        self.events = EventRecorder(ctx.settings, ctx.clock)

    # ================================================================== drift

    def drift(self, run_id: str) -> list[DriftItem]:
        with self.ctx.session() as session:
            run = get_or_404(session, Run, run_id, "run")
            return self._drift(session, run)

    def _drift(self, session: Session, run: Run) -> list[DriftItem]:
        snap = dict(run.snapshot or {})
        project_snap = snap.get("project") or {}
        project = session.get(Project, run.project_id) if run.project_id else None
        current_git: dict[str, Any] | None = None
        current_settings: dict[str, Any] | None = None
        if project is not None:
            current_git = capture_git_state(project.repo_path).as_dict()
            current_settings = dict(project.settings or {})
        row = session.scalars(select(RunWorkspace).where(RunWorkspace.run_id == run.id)).first()
        adapter_type = (snap.get("agent_profile") or {}).get("adapter_type")
        current_version: str | None = None
        if adapter_type and self.ctx.adapters.has(adapter_type):
            try:
                current_version = self.ctx.adapters.get(adapter_type).discover().version
            except Exception:
                current_version = None
        return compute_drift(
            snap,
            current_git=current_git,
            current_project_settings=current_settings,
            project_exists=project is not None or not project_snap,
            original_adapter_version=row.adapter_version if row else None,
            current_adapter_version=current_version,
            current_environment=environment_fingerprint(),
        )

    # ================================================================== create

    def _base_request(self, run: Run) -> RunCreate:
        if run.kind not in REPLAYABLE_KINDS:
            raise ConflictError(
                f"Replay and fork are available for agent runs (this run is a {run.kind} run)"
            )
        snap = dict(run.snapshot or {})
        prompt = snap.get("prompt") or {}
        profile = snap.get("agent_profile") or {}
        options = snap.get("options") or dict(run.options or {})
        if not profile.get("profile_id") or not profile.get("version_id"):
            raise ConflictError("The original run has no agent profile in its snapshot")
        evaluators = [EvaluatorConfig.model_validate(e) for e in options.get("evaluators") or []]
        req = RunCreate(
            project_id=run.project_id,
            agent_profile_id=profile["profile_id"],
            agent_profile_version_id=profile["version_id"],
            inputs=dict(snap.get("inputs") or run.inputs or {}),
            priority=run.priority,
            evaluators=evaluators,
            max_attempts=run.max_attempts,
            timeout_seconds=options.get("timeout_seconds"),
            idle_timeout_seconds=options.get("idle_timeout_seconds"),
            budget=dict(run.budget) if run.budget else None,
            tags=[t for t in (run.tags or []) if isinstance(t, str)],
            parent_run_id=run.id,
        )
        if prompt.get("template_id"):
            req.prompt_template_id = prompt["template_id"]
            req.prompt_version_id = prompt.get("version_id")
        elif prompt.get("inline"):
            req.prompt = str(prompt.get("body") or "")
        return req

    def _workflow_request(self, run: Run) -> RunCreate:
        """Replay a workflow run: the same immutable version (definition) and inputs."""
        snap = dict(run.snapshot or {})
        workflow = dict(snap.get("workflow") or {})
        if not workflow.get("definition") or not run.workflow_version_id:
            raise ConflictError("The original run has no workflow definition in its snapshot")
        return RunCreate(
            kind="workflow",
            project_id=run.project_id,
            workflow_id=run.workflow_id,
            workflow_version_id=run.workflow_version_id,
            inputs=dict(snap.get("inputs") or run.inputs or {}),
            priority=run.priority,
            tags=[t for t in (run.tags or []) if isinstance(t, str)],
            parent_run_id=run.id,
            max_attempts=1,
            extra_snapshot={"workflow": workflow},
        )

    def replay(
        self, run_id: str, body: ReplayRequest | None = None, *, actor: str = "local user"
    ) -> LineageCreateResult:
        body = body or ReplayRequest()
        with self.ctx.session() as session:
            run = get_or_404(session, Run, run_id, "run")
            drift = self._drift(session, run)
            req = self._workflow_request(run) if run.kind == "workflow" else self._base_request(run)
            title = body.title or f"Replay of {run.title}"
            is_demo = run.is_demo
        req = req.model_copy(
            update={
                "title": title[:300],
                "notes": body.notes,
                "source": RunSource.REPLAY,
                "source_ref": run_id,
                "parent_relation": "replay",
                "is_demo": is_demo or None,
                "extra_snapshot": {
                    **req.extra_snapshot,
                    "replay": {
                        "of_run_id": run_id,
                        "drift": [d.model_dump(mode="json") for d in drift],
                    },
                },
            }
        )
        result = self.runs.create_run(req, actor=actor)
        self._record(result.run.id, "replay", run_id, drift, [])
        return LineageCreateResult(result=result, drift=drift, changes=[])

    def fork(
        self, run_id: str, body: ForkRequest, *, actor: str = "local user"
    ) -> LineageCreateResult:
        with self.ctx.session() as session:
            run = get_or_404(session, Run, run_id, "run")
            drift = self._drift(session, run)
            req = self._base_request(run)
            snap = dict(run.snapshot or {})
            changes = self._apply_fork(session, req, snap, body)
            title = body.title or f"Fork of {run.title}"
            is_demo = run.is_demo
        if not changes:
            raise ValidationError(
                "A fork needs at least one change (prompt version, agent profile, or a "
                "variable); use replay to run the same configuration again"
            )
        req = req.model_copy(
            update={
                "title": title[:300],
                "notes": body.notes,
                "source": RunSource.FORK,
                "source_ref": run_id,
                "parent_relation": "fork",
                "is_demo": is_demo or None,
                "extra_snapshot": {
                    "fork": {
                        "of_run_id": run_id,
                        "changes": changes,
                        "drift": [d.model_dump(mode="json") for d in drift],
                    }
                },
            }
        )
        result = self.runs.create_run(req, actor=actor)
        self._record(result.run.id, "fork", run_id, drift, changes)
        return LineageCreateResult(result=result, drift=drift, changes=changes)

    def _apply_fork(
        self, session: Session, req: RunCreate, snap: dict[str, Any], body: ForkRequest
    ) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        prompt = snap.get("prompt") or {}
        profile = snap.get("agent_profile") or {}
        if body.prompt_version_id and body.prompt_version_id != prompt.get("version_id"):
            if not prompt.get("template_id"):
                raise ValidationError("The original run used an inline prompt; it has no versions")
            version = get_or_404(session, PromptVersion, body.prompt_version_id, "prompt version")
            if version.template_id != prompt["template_id"]:
                raise ValidationError("The prompt version must belong to the original template")
            req.prompt_version_id = version.id
            changes.append(
                {
                    "field": "prompt_version",
                    "from": prompt.get("version"),
                    "to": version.version,
                    "to_id": version.id,
                }
            )
        target_profile = body.agent_profile_id or profile.get("profile_id")
        if body.agent_profile_version_id or (
            body.agent_profile_id and body.agent_profile_id != profile.get("profile_id")
        ):
            prof = get_or_404(session, AgentProfile, str(target_profile), "agent profile")
            pversion: AgentProfileVersion | None
            if body.agent_profile_version_id:
                pversion = get_or_404(
                    session, AgentProfileVersion, body.agent_profile_version_id, "profile version"
                )
                if pversion.profile_id != prof.id:
                    raise ValidationError("The profile version must belong to the chosen profile")
            else:
                pversion = session.scalars(
                    select(AgentProfileVersion)
                    .where(AgentProfileVersion.profile_id == prof.id)
                    .order_by(AgentProfileVersion.version.desc())
                ).first()
            if pversion is None:
                raise ValidationError("The chosen agent profile has no versions")
            if pversion.id != profile.get("version_id"):
                req.agent_profile_id = prof.id
                req.agent_profile_version_id = pversion.id
                changes.append(
                    {
                        "field": "agent_profile",
                        "from": f"{profile.get('name')} v{profile.get('version')}",
                        "to": f"{prof.name} v{pversion.version}",
                        "to_id": pversion.id,
                    }
                )
                # A different profile brings its own evaluators unless the run pinned some.
                if prof.id != profile.get("profile_id"):
                    req.evaluators = None
        original_inputs = dict(snap.get("inputs") or {})
        for key, value in body.inputs.items():
            if original_inputs.get(key) != value:
                changes.append(
                    {"field": f"inputs.{key}", "from": original_inputs.get(key), "to": value}
                )
        req.inputs = {**original_inputs, **body.inputs}
        return changes

    def _record(
        self,
        new_run_id: str,
        relation: str,
        original_id: str,
        drift: list[DriftItem],
        changes: list[dict[str, Any]],
    ) -> None:
        with self.ctx.session() as session:
            if changes:
                self.events.record(
                    session,
                    new_run_id,
                    EventType.LIFECYCLE,
                    f"Forked from run {original_id[:8]} with {len(changes)} change(s)",
                    {"relation": relation, "of_run_id": original_id, "changes": changes},
                )
            if drift:
                self.events.record(
                    session,
                    new_run_id,
                    EventType.WARNING,
                    "Environment differs from the original run: "
                    + "; ".join(d.label for d in drift),
                    {
                        "kind": "replay_drift",
                        "of_run_id": original_id,
                        "drift": [d.model_dump(mode="json") for d in drift],
                    },
                    severity=Severity.WARNING,
                )

    # ================================================================== read

    def lineage(self, run_id: str) -> LineageOut:
        with self.ctx.session() as session:
            run = get_or_404(session, Run, run_id, "run")
            snap = dict(run.snapshot or {})
            parent = session.get(Run, run.parent_run_id) if run.parent_run_id else None
            children = session.scalars(
                select(Run).where(Run.parent_run_id == run_id).order_by(Run.created_at)
            ).all()
            return LineageOut(
                run_id=run.id,
                parent=_lineage_run(parent, run.parent_relation) if parent else None,
                children=[_lineage_run(c, c.parent_relation) for c in children],
                drift=self._drift(session, run),
                options=self._options(session, run, snap),
                replay_of=snap.get("replay"),
                fork_of=snap.get("fork"),
            )

    def _options(self, session: Session, run: Run, snap: dict[str, Any]) -> ForkOptions:
        prompt = snap.get("prompt") or {}
        profile = snap.get("agent_profile") or {}
        replayable, reason = True, None
        forkable, fork_reason = run.kind in REPLAYABLE_KINDS, None
        if run.kind == "workflow":
            fork_reason = "Fork is available for agent runs; replay reruns this workflow version"
            if not run.workflow_version_id or not (snap.get("workflow") or {}).get("definition"):
                replayable, reason = False, "The original run has no workflow definition snapshot"
        elif run.kind not in REPLAYABLE_KINDS:
            replayable, reason = False, "Replay and fork are available for agent runs"
        elif not profile.get("version_id"):
            replayable, reason = False, "The original run has no agent profile snapshot"
        elif (
            profile.get("version_id")
            and session.get(AgentProfileVersion, profile["version_id"]) is None
        ):
            replayable, reason = False, "The original agent profile version no longer exists"
        elif prompt.get("version_id") and session.get(PromptVersion, prompt["version_id"]) is None:
            replayable, reason = False, "The original prompt version no longer exists"
        prompt_versions: list[VersionChoice] = []
        if prompt.get("template_id"):
            for v in session.scalars(
                select(PromptVersion)
                .where(PromptVersion.template_id == prompt["template_id"])
                .order_by(PromptVersion.version.desc())
                .limit(50)
            ):
                prompt_versions.append(
                    VersionChoice(
                        id=v.id,
                        version=v.version,
                        label=f"v{v.version}" + (f" ({v.notes})" if v.notes else ""),
                        created_at=v.created_at,
                    )
                )
        profiles: list[ProfileChoice] = []
        stmt = (
            select(AgentProfile)
            .where(AgentProfile.archived.is_(False))
            .order_by(AgentProfile.name)
            .limit(100)
        )
        for p in session.scalars(stmt):
            versions = session.scalars(
                select(AgentProfileVersion)
                .where(AgentProfileVersion.profile_id == p.id)
                .order_by(AgentProfileVersion.version.desc())
                .limit(20)
            ).all()
            profiles.append(
                ProfileChoice(
                    id=p.id,
                    name=p.name,
                    adapter_type=p.adapter_type,
                    versions=[
                        VersionChoice(
                            id=v.id,
                            version=v.version,
                            label=f"v{v.version}" + (f" ({v.notes})" if v.notes else ""),
                            created_at=v.created_at,
                        )
                        for v in versions
                    ],
                )
            )
        return ForkOptions(
            replayable=replayable,
            reason=reason,
            forkable=forkable and replayable,
            fork_reason=fork_reason or reason,
            prompt_template_id=prompt.get("template_id"),
            prompt_name=prompt.get("name") or ("Inline prompt" if prompt else None),
            prompt_version_id=prompt.get("version_id"),
            prompt_versions=prompt_versions,
            variables=list(prompt.get("variables") or []),
            inputs=dict(snap.get("inputs") or {}),
            agent_profile_id=profile.get("profile_id"),
            agent_profile_version_id=profile.get("version_id"),
            profiles=profiles,
        )


def _lineage_run(run: Run, relation: str | None) -> LineageRun:
    return LineageRun(
        id=run.id,
        title=run.title,
        status=run.status,
        source=run.source,
        relation=relation,
        created_at=run.created_at,
    )
