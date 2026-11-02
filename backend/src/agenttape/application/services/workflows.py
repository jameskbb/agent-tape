"""WorkflowService: versioned DAG definitions and starting workflow runs.

A workflow's versions are immutable: every save creates version N+1, and the first run
that uses a version locks it (``locked_at``). Each version is stored twice: the full
``definition`` JSON (the source of truth, also copied into every run's snapshot) and
normalized ``workflow_nodes`` / ``workflow_edges`` rows for querying.

Starting a workflow creates an ordinary run (``kind="workflow"``) through
``RunService.create_run`` - the same engine every other trigger uses. Schedules and
webhooks build the same request with ``schedules.workflow_run_fields``; the engine applies
input defaults and locks the version for those runs too. See docs/ORCHESTRATION.md.

This module also registers the ``workflow_node`` approval handler, which wakes the
workflow engine after a decision (the engine reads the decision from the approval row).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agenttape.application.context import AppContext
from agenttape.application.events import EventRecorder
from agenttape.application.schemas.common import Page
from agenttape.application.schemas.runs import RunCreate, RunCreateResult
from agenttape.application.schemas.workflows import (
    NodeTypeInfo,
    ValidationIssue,
    WorkflowCreate,
    WorkflowOut,
    WorkflowRunCreate,
    WorkflowUpdate,
    WorkflowValidation,
    WorkflowVersionCreate,
    WorkflowVersionOut,
)
from agenttape.application.services.approvals import register_approval_handler
from agenttape.application.services.audit import record_audit
from agenttape.application.services.common import get_or_404, hash_json, like_pattern, paginate
from agenttape.application.services.workspaces import default_workspace_id
from agenttape.application.transitions import transition_run
from agenttape.domain.enums import EventType, RunSource, RunStatus
from agenttape.domain.errors import ConflictError, ValidationError
from agenttape.domain.prompts import validate_inputs
from agenttape.domain.workflows import (
    NODE_TYPE_LABELS,
    Issue,
    NodeType,
    WorkflowDefinition,
    topological_order,
    validate_definition,
)
from agenttape.infrastructure.db.models import (
    AgentProfile,
    ApprovalRequest,
    EvaluationDefinition,
    EvaluationSuite,
    Project,
    PromptTemplate,
    Run,
    Workflow,
    WorkflowEdge,
    WorkflowNode,
    WorkflowVersion,
)

WORKFLOW_KIND = "workflow"
APPROVAL_HANDLER = "workflow_node"
CANCEL_PRIORITY = 100
SETTLED = frozenset({RunStatus.SUCCEEDED.value, RunStatus.FAILED.value, RunStatus.TIMED_OUT.value})

NODE_TYPE_HELP: dict[NodeType, tuple[str, list[dict[str, Any]]]] = {
    NodeType.AGENT_RUN: (
        "Runs an agent profile as a child run through the normal run engine. Its outputs "
        "(plus child_run_id, status, workdir) become this node's outputs.",
        [
            {"name": "agent_profile_id", "kind": "agent_profile", "required": True},
            {"name": "prompt_template_id", "kind": "prompt_template"},
            {"name": "prompt", "kind": "text", "help": "Inline prompt (templates allowed)"},
            {"name": "evaluators", "kind": "json", "help": "Evaluators for the child run"},
        ],
    ),
    NodeType.COMMAND: (
        "Runs a program with an argument array (never a shell). The executable must be on "
        "the allowlist or the node pauses for an elevated-command approval.",
        [
            {"name": "argv", "kind": "argv", "required": True},
            {"name": "cwd", "kind": "text", "help": "Working directory (template allowed)"},
            {"name": "elevated", "kind": "boolean"},
            {"name": "parse_json", "kind": "boolean", "help": "Parse stdout as JSON output"},
            {"name": "expected_exit_codes", "kind": "json"},
        ],
    ),
    NodeType.TRANSFORM: (
        "Renders text or reshapes data from earlier outputs with the safe template syntax.",
        [
            {"name": "template", "kind": "text", "help": "Rendered into outputs.text"},
            {"name": "values", "kind": "json", "help": "Object rendered into outputs"},
        ],
    ),
    NodeType.EVALUATION_GATE: (
        "Runs evaluators against a target node's child run (or the workflow run). Fails "
        "when a required evaluation fails, unless an approved override continues.",
        [
            {"name": "target", "kind": "node", "help": "Node whose child run is evaluated"},
            {"name": "evaluators", "kind": "json"},
            {"name": "definition_ids", "kind": "json"},
            {"name": "suite_id", "kind": "text"},
            {"name": "allow_override", "kind": "boolean"},
        ],
    ),
    NodeType.APPROVAL: (
        "Pauses the workflow durably until a person approves or rejects in the inbox.",
        [
            {"name": "title", "kind": "text", "required": True},
            {"name": "rationale", "kind": "text"},
            {
                "name": "risk_level",
                "kind": "enum",
                "options": ["low", "medium", "high", "critical"],
            },
            {"name": "expires_in_seconds", "kind": "number"},
            {"name": "requested_action", "kind": "json"},
        ],
    ),
    NodeType.CONDITION: (
        "Evaluates an expression; edges leaving it follow the true or false branch.",
        [{"name": "expression", "kind": "text", "required": True}],
    ),
    NodeType.COLLECT: (
        "Collects outputs of upstream nodes into the workflow result and a JSON artifact.",
        [
            {"name": "include", "kind": "json", "help": "Node keys (default: all upstream)"},
            {"name": "values", "kind": "json"},
            {"name": "artifact_name", "kind": "text"},
        ],
    ),
    NodeType.FINDING_REVIEW: (
        "Challenges an upstream agent run node's findings: starts a reviewer child run with "
        "the chosen agent profile that re-derives each finding and records upheld, refuted, "
        "or inconclusive as model judgment (never proof). Its outputs (plus child_run_id and "
        "status) become this node's outputs.",
        [
            {
                "name": "target",
                "kind": "node",
                "required": True,
                "help": "Agent run node whose findings to challenge",
            },
            {"name": "agent_profile_id", "kind": "agent_profile", "required": True},
            {
                "name": "agent_profile_version_id",
                "kind": "text",
                "help": "Pin a profile version (default: latest)",
            },
        ],
    ),
}


def node_types() -> list[NodeTypeInfo]:
    return [
        NodeTypeInfo(
            type=t.value, label=NODE_TYPE_LABELS[t], description=desc, config_fields=fields
        )
        for t, (desc, fields) in NODE_TYPE_HELP.items()
    ]


def definition_hash(definition: dict[str, Any]) -> str:
    return hash_json(definition)


def wake_workflow(ctx: AppContext, session: Session, run: Run, reason: str) -> None:
    """Put a workflow run back on the queue so the engine re-evaluates it."""
    from agenttape.application.services.runs import RunService

    if run.kind != WORKFLOW_KIND or run.status in SETTLED or run.status == "skipped":
        return
    if run.status == RunStatus.WAITING_FOR_APPROVAL.value:
        transition_run(session, run, RunStatus.QUEUED, queued_at=ctx.clock.now())
        EventRecorder(ctx.settings, ctx.clock).record(
            session, run.id, EventType.LIFECYCLE, reason, {"status": "queued"}
        )
    RunService(ctx).enqueue(session, run)


def _approval_handler(
    ctx: AppContext, session: Session, approval: ApprovalRequest, approved: bool
) -> None:
    if approval.run_id is None:
        return
    run = session.get(Run, approval.run_id)
    if run is None:
        return
    decision = "approved" if approved else approval.status
    wake_workflow(ctx, session, run, f"Workflow resuming: approval {decision}")


def _on_cancel(ctx: AppContext, session: Session, run: Run) -> None:
    """Cancelling a workflow run (or one of its children) wakes the engine to propagate it."""
    if run.kind == WORKFLOW_KIND:
        # Propagation jumps the queue so queued children are cancelled before they start.
        run.queue_generation = (run.queue_generation or 0) + 1
        session.flush()
        ctx.queue.enqueue(
            session,
            dedupe_key=f"run:{run.id}:{run.queue_generation}",
            run_id=run.id,
            priority=CANCEL_PRIORITY,
            kind="run",
        )
        return
    if run.parent_relation == "workflow_node" and run.parent_run_id:
        parent = session.get(Run, run.parent_run_id)
        if parent is not None and parent.kind == WORKFLOW_KIND:
            wake_workflow(ctx, session, parent, "Workflow resuming: a child run was cancelled")


def _register_hooks() -> None:
    from agenttape.application.services.runs import register_cancel_hook

    register_approval_handler(APPROVAL_HANDLER, _approval_handler)
    register_cancel_hook(_on_cancel)


_register_hooks()


class WorkflowService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------------ validation

    def validate(self, definition: dict[str, Any]) -> WorkflowValidation:
        with self.ctx.session() as session:
            return self._validate(session, definition)

    def _validate(self, session: Session, raw: dict[str, Any]) -> WorkflowValidation:
        issues = validate_definition(raw)
        order: list[str] | None = None
        if not any(i.severity == "error" for i in issues):
            definition = WorkflowDefinition.model_validate(raw)
            issues.extend(self._reference_issues(session, definition))
            order = topological_order(
                [n.key for n in definition.nodes],
                [(e.source, e.target) for e in definition.edges],
            )
        return WorkflowValidation(
            valid=not any(i.severity == "error" for i in issues),
            issues=[ValidationIssue(**i.as_dict()) for i in issues],
            order=order,
        )

    def _reference_issues(self, session: Session, definition: WorkflowDefinition) -> list[Issue]:
        """Referenced profiles, prompts, evaluation definitions, and projects must exist."""
        issues: list[Issue] = []
        pid = definition.settings.project_id
        if pid and session.get(Project, pid) is None:
            issues.append(Issue("settings.project_id", f"Project '{pid}' does not exist"))
        for i, node in enumerate(definition.nodes):
            c = node.config
            where = f"nodes[{i}].config"
            if node.type in (NodeType.AGENT_RUN, NodeType.FINDING_REVIEW):
                prof = c.get("agent_profile_id")
                profile = session.get(AgentProfile, prof) if isinstance(prof, str) else None
                if prof and profile is None:
                    issues.append(Issue(where, f"Agent profile '{prof}' does not exist"))
                elif profile is not None and not self.ctx.adapters.has(profile.adapter_type):
                    issues.append(
                        Issue(where, f"Adapter '{profile.adapter_type}' is not installed")
                    )
                tpl = c.get("prompt_template_id")
                if tpl and session.get(PromptTemplate, tpl) is None:
                    issues.append(Issue(where, f"Prompt template '{tpl}' does not exist"))
            if node.type == NodeType.EVALUATION_GATE:
                for def_id in c.get("definition_ids") or []:
                    if session.get(EvaluationDefinition, def_id) is None:
                        issues.append(Issue(where, f"Evaluation definition '{def_id}' not found"))
                suite = c.get("suite_id")
                if suite and session.get(EvaluationSuite, suite) is None:
                    issues.append(Issue(where, f"Evaluation suite '{suite}' not found"))
                for spec in c.get("evaluators") or []:
                    if isinstance(spec, dict) and not self.ctx.evaluators.has(
                        str(spec.get("type"))
                    ):
                        issues.append(Issue(where, f"Unknown evaluator type '{spec.get('type')}'"))
                target = c.get("target")
                if target and target not in definition.node_map():
                    issues.append(Issue(where, f"Gate target '{target}' is not a node"))
            if node.type == NodeType.COLLECT:
                for key in c.get("include") or []:
                    if key not in definition.node_map():
                        issues.append(Issue(where, f"'{key}' is not a node"))
        return issues

    def _require_valid(self, session: Session, raw: dict[str, Any]) -> WorkflowDefinition:
        result = self._validate(session, raw)
        if not result.valid:
            raise ValidationError(
                "The workflow definition is invalid",
                details={"issues": [i.model_dump() for i in result.issues]},
            )
        return WorkflowDefinition.model_validate(raw)

    # ------------------------------------------------------------------ create / versions

    def create(
        self, data: WorkflowCreate, *, actor: str = "local user", is_demo: bool = False
    ) -> WorkflowOut:
        with self.ctx.session() as session:
            definition = self._require_valid(session, data.definition)
            ws_id = default_workspace_id(session, self.ctx.clock)
            if session.scalars(
                select(Workflow).where(Workflow.workspace_id == ws_id, Workflow.name == data.name)
            ).first():
                raise ConflictError(f"A workflow named '{data.name}' already exists")
            now = self.ctx.clock.now()
            workflow = Workflow(
                workspace_id=ws_id,
                project_id=data.project_id or definition.settings.project_id,
                name=data.name,
                description=data.description,
                tags=data.tags,
                latest_version=0,
                is_demo=is_demo,
                created_at=now,
                updated_at=now,
            )
            session.add(workflow)
            session.flush()
            self._add_version(session, workflow, definition, data.notes, actor=actor)
            record_audit(
                session,
                self.ctx.clock,
                action="workflow.create",
                resource_type="workflow",
                resource_id=workflow.id,
                actor=actor,
                workspace_id=ws_id,
                details={"name": data.name, "nodes": len(definition.nodes)},
            )
            return self._to_out(session, workflow)

    def _add_version(
        self,
        session: Session,
        workflow: Workflow,
        definition: WorkflowDefinition,
        notes: str | None,
        *,
        actor: str,
    ) -> WorkflowVersion:
        dumped = definition.dump()
        now = self.ctx.clock.now()
        workflow.latest_version += 1
        version = WorkflowVersion(
            workflow_id=workflow.id,
            version=workflow.latest_version,
            definition=dumped,
            input_schema={"variables": dumped["inputs"]},
            content_hash=definition_hash(dumped),
            notes=notes,
            created_by=actor,
            created_at=now,
        )
        session.add(version)
        session.flush()
        for node in definition.nodes:
            session.add(
                WorkflowNode(
                    workflow_version_id=version.id,
                    node_key=node.key,
                    node_type=node.type.value,
                    name=node.label,
                    description=node.description,
                    config=node.config,
                    input_schema=node.input_schema,
                    output_schema=node.output_schema,
                    timeout_seconds=node.timeout_seconds,
                    max_attempts=node.max_attempts,
                    retry={"backoff_seconds": node.retry_backoff_seconds},
                    continue_on_failure=node.continue_on_failure,
                    required=node.required,
                    position=node.position,
                )
            )
        for edge in definition.edges:
            session.add(
                WorkflowEdge(
                    workflow_version_id=version.id,
                    from_node_key=edge.source,
                    to_node_key=edge.target,
                    condition=edge.condition.model_dump(exclude_none=True)
                    if edge.condition
                    else None,
                )
            )
        workflow.updated_at = now
        session.flush()
        return version

    def create_version(
        self, workflow_id: str, data: WorkflowVersionCreate, *, actor: str = "local user"
    ) -> WorkflowVersionOut:
        with self.ctx.session() as session:
            workflow = get_or_404(session, Workflow, workflow_id, "workflow")
            if workflow.archived:
                raise ConflictError("Archived workflows cannot get new versions")
            definition = self._require_valid(session, data.definition)
            latest = self._latest(session, workflow.id)
            if latest is not None and latest.content_hash == definition_hash(definition.dump()):
                raise ConflictError(
                    f"No changes since version {latest.version}",
                    details={"version": latest.version},
                )
            version = self._add_version(session, workflow, definition, data.notes, actor=actor)
            record_audit(
                session,
                self.ctx.clock,
                action="workflow.version",
                resource_type="workflow",
                resource_id=workflow.id,
                actor=actor,
                workspace_id=workflow.workspace_id,
                details={"version": version.version},
            )
            return WorkflowVersionOut.model_validate(version)

    def update(
        self, workflow_id: str, data: WorkflowUpdate, *, actor: str = "local user"
    ) -> WorkflowOut:
        with self.ctx.session() as session:
            workflow = get_or_404(session, Workflow, workflow_id, "workflow")
            changes = data.model_dump(exclude_unset=True)
            if "name" in changes and changes["name"] != workflow.name:
                clash = session.scalars(
                    select(Workflow).where(
                        Workflow.workspace_id == workflow.workspace_id,
                        Workflow.name == changes["name"],
                    )
                ).first()
                if clash:
                    raise ConflictError(f"A workflow named '{changes['name']}' already exists")
            for key, value in changes.items():
                setattr(workflow, key, value)
            workflow.updated_at = self.ctx.clock.now()
            record_audit(
                session,
                self.ctx.clock,
                action="workflow.update",
                resource_type="workflow",
                resource_id=workflow.id,
                actor=actor,
                workspace_id=workflow.workspace_id,
                details={"fields": sorted(changes)},
            )
            session.flush()
            return self._to_out(session, workflow)

    # ------------------------------------------------------------------ queries

    @staticmethod
    def _latest(session: Session, workflow_id: str) -> WorkflowVersion | None:
        return session.scalars(
            select(WorkflowVersion)
            .where(WorkflowVersion.workflow_id == workflow_id)
            .order_by(WorkflowVersion.version.desc())
            .limit(1)
        ).first()

    def _to_out(self, session: Session, workflow: Workflow) -> WorkflowOut:
        out = WorkflowOut.model_validate(workflow)
        latest = self._latest(session, workflow.id)
        out.latest = WorkflowVersionOut.model_validate(latest) if latest else None
        out.node_count = len((latest.definition or {}).get("nodes") or []) if latest else 0
        last = session.scalars(
            select(Run)
            .where(Run.workflow_id == workflow.id, Run.kind == WORKFLOW_KIND)
            .order_by(Run.created_at.desc())
            .limit(1)
        ).first()
        if last is not None:
            out.last_run_id, out.last_run_status, out.last_run_at = (
                last.id,
                last.status,
                last.created_at,
            )
        return out

    def get(self, workflow_id: str) -> WorkflowOut:
        with self.ctx.session() as session:
            return self._to_out(session, get_or_404(session, Workflow, workflow_id, "workflow"))

    def list_workflows(
        self,
        *,
        search: str | None = None,
        include_archived: bool = False,
        project_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Page[WorkflowOut]:
        with self.ctx.session() as session:
            stmt = select(Workflow)
            if not include_archived:
                stmt = stmt.where(Workflow.archived.is_(False))
            if project_id:
                stmt = stmt.where(Workflow.project_id == project_id)
            if search:
                stmt = stmt.where(func.lower(Workflow.name).like(like_pattern(search), escape="\\"))
            stmt = stmt.order_by(Workflow.updated_at.desc(), Workflow.id.desc())
            return paginate(
                session, stmt, limit=limit, offset=offset, mapper=lambda w: self._to_out(session, w)
            )

    def list_versions(self, workflow_id: str) -> list[WorkflowVersionOut]:
        with self.ctx.session() as session:
            get_or_404(session, Workflow, workflow_id, "workflow")
            rows = session.scalars(
                select(WorkflowVersion)
                .where(WorkflowVersion.workflow_id == workflow_id)
                .order_by(WorkflowVersion.version.desc())
            ).all()
            return [WorkflowVersionOut.model_validate(v) for v in rows]

    def get_version(self, workflow_id: str, version_id: str) -> WorkflowVersionOut:
        with self.ctx.session() as session:
            version = get_or_404(session, WorkflowVersion, version_id, "workflow version")
            if version.workflow_id != workflow_id:
                raise ValidationError("That version belongs to another workflow")
            return WorkflowVersionOut.model_validate(version)

    # ------------------------------------------------------------------ runs

    def start_run(
        self,
        workflow_id: str,
        data: WorkflowRunCreate,
        *,
        source: RunSource = RunSource.API,
        source_ref: str | None = None,
        schedule_id: str | None = None,
        trigger_id: str | None = None,
        actor: str = "local user",
        is_demo: bool | None = None,
    ) -> RunCreateResult:
        """Create a workflow run through ``RunService.create_run`` (the one run path)."""
        from agenttape.application.services.runs import RunService

        with self.ctx.session() as session:
            workflow = get_or_404(session, Workflow, workflow_id, "workflow")
            if workflow.archived:
                raise ConflictError("Cannot start an archived workflow")
            if data.version_id:
                version = get_or_404(session, WorkflowVersion, data.version_id, "workflow version")
                if version.workflow_id != workflow.id:
                    raise ValidationError("That version belongs to another workflow")
            else:
                latest = self._latest(session, workflow.id)
                if latest is None:
                    raise ConflictError("The workflow has no versions")
                version = latest
            definition = self._require_valid(session, dict(version.definition))
            values, issues = validate_inputs(definition.inputs, data.inputs)
            if issues:
                raise ValidationError(
                    "Workflow inputs are invalid",
                    details={"issues": [i.model_dump() for i in issues]},
                )
            snapshot_section = {
                "workflow_id": workflow.id,
                "name": workflow.name,
                "version_id": version.id,
                "version": version.version,
                "content_hash": version.content_hash,
                "definition": dict(version.definition),
            }
            project_id = definition.settings.project_id or workflow.project_id
            demo = workflow.is_demo if is_demo is None else is_demo
            version_id, version_no, name = version.id, version.version, workflow.name
        result = RunService(self.ctx).create_run(
            RunCreate(
                kind=WORKFLOW_KIND,
                title=data.title or f"{name} (v{version_no})",
                project_id=project_id,
                inputs=values,
                priority=data.priority,
                idempotency_key=data.idempotency_key,
                source=source,
                source_ref=source_ref,
                tags=data.tags,
                notes=data.notes,
                workflow_id=workflow_id,
                workflow_version_id=version_id,
                schedule_id=schedule_id,
                trigger_id=trigger_id,
                extra_snapshot={"workflow": snapshot_section},
                is_demo=demo,
                max_attempts=1,
            ),
            actor=actor,
        )
        if result.created:
            with self.ctx.session() as session:
                version = get_or_404(session, WorkflowVersion, version_id, "workflow version")
                if version.locked_at is None:
                    version.locked_at = self.ctx.clock.now()
        return result
