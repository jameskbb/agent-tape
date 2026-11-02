"""Run, attempt, step, event, artifact, file-change, and evaluation DTOs."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from agenttape.application.schemas.common import OrmModel
from agenttape.domain.enums import RunSource, RunStatus


class EvaluatorConfig(BaseModel):
    type: str
    name: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    required: bool = False
    definition_id: str | None = None
    definition_version_id: str | None = None


class RunCreate(BaseModel):
    """Create a run. The same request shape is used by UI, API, CLI, schedules, webhooks.

    Provide either ``prompt_template_id`` (optionally pinned by ``prompt_version_id``) or an
    inline ``prompt``; ``agent_profile_id`` selects the agent (latest version unless
    ``agent_profile_version_id`` pins one).
    """

    kind: str = "agent"
    title: str | None = Field(default=None, max_length=300)
    project_id: str | None = None
    agent_profile_id: str | None = None
    agent_profile_version_id: str | None = None
    prompt_template_id: str | None = None
    prompt_version_id: str | None = None
    prompt: str | None = Field(default=None, max_length=200_000)
    inputs: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=0, ge=-100, le=100)
    idempotency_key: str | None = Field(default=None, max_length=255)
    source: RunSource = RunSource.API
    source_ref: str | None = Field(default=None, max_length=255)
    tags: list[str] = Field(default_factory=list)
    notes: str | None = None
    evaluators: list[EvaluatorConfig] | None = None
    max_attempts: int | None = Field(default=None, ge=1, le=20)
    timeout_seconds: float | None = Field(default=None, gt=0)
    idle_timeout_seconds: float | None = Field(default=None, gt=0)
    budget: dict[str, Any] | None = None
    #: Linkage (replay/fork/workflow child runs) and external origins.
    parent_run_id: str | None = None
    parent_relation: str | None = None
    parent_node_key: str | None = None
    workflow_id: str | None = None
    workflow_version_id: str | None = None
    schedule_id: str | None = None
    trigger_id: str | None = None
    #: Extra immutable snapshot sections contributed by the caller (e.g. workflow engine).
    extra_snapshot: dict[str, Any] = Field(default_factory=dict)
    #: Force demo tagging (otherwise inherited from demo project/profile).
    is_demo: bool | None = None
    #: Create in ``queued`` state but do not enqueue (caller enqueues later, e.g. workflows).
    defer_enqueue: bool = False


class RunOut(OrmModel):
    id: str
    workspace_id: str
    project_id: str | None
    title: str
    kind: str
    source: str
    source_ref: str | None
    idempotency_key: str | None
    status: RunStatus
    failure_category: str | None
    failure_message: str | None
    priority: int
    adapter_type: str | None
    prompt_template_id: str | None
    prompt_version_id: str | None
    agent_profile_id: str | None
    agent_profile_version_id: str | None
    workflow_id: str | None
    workflow_version_id: str | None
    schedule_id: str | None
    trigger_id: str | None
    parent_run_id: str | None
    parent_relation: str | None
    parent_node_key: str | None
    tags: list[str]
    notes: str | None
    max_attempts: int
    attempt_count: int
    queued_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    cancel_requested_at: datetime | None
    usage: dict[str, Any] | None
    cost: dict[str, Any] | None
    is_demo: bool
    created_at: datetime
    updated_at: datetime
    created_by: str | None
    #: Filled for list/detail responses when the run is waiting in the queue.
    queue_position: int | None = None
    #: Names for display (resolved from snapshot, so they survive deletions).
    prompt_name: str | None = None
    agent_profile_name: str | None = None
    project_name: str | None = None
    #: How this run's agent records findings, resolved from the adapter and the run's own
    #: profile snapshot (``AgentAdapter.findings_capture_for``): an adapter hosting several
    #: agents (Herdr) answers per agent, not once per adapter. ``None`` for a run with no
    #: agent profile or whose adapter is no longer installed.
    findings_capture: Literal["mcp", "scripted", "unsupported"] | None = None


class RunCreateResult(BaseModel):
    run: RunOut
    created: bool


class AttemptOut(OrmModel):
    id: str
    run_id: str
    attempt_number: int
    status: RunStatus
    failure_category: str | None
    failure_message: str | None
    worker_id: str | None
    exit_code: int | None
    workdir: str | None
    stdout_bytes: int
    stderr_bytes: int
    outputs: dict[str, Any] | None
    usage: dict[str, Any] | None
    cost: dict[str, Any] | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    heartbeat_at: datetime | None
    retry_scheduled_at: datetime | None
    has_checkpoint: bool = False


class StepRunOut(OrmModel):
    id: str
    run_id: str
    node_key: str
    node_type: str
    name: str
    status: RunStatus
    position: int
    attempt_count: int
    child_run_id: str | None
    approval_request_id: str | None
    inputs: dict[str, Any] | None
    outputs: dict[str, Any] | None
    failure_category: str | None
    failure_message: str | None
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None


class EventOut(OrmModel):
    id: str
    run_id: str
    attempt_id: str | None
    step_run_id: str | None
    node_key: str | None
    parent_event_id: str | None
    sequence: int
    ts: datetime
    monotonic_ms: float
    type: str
    source: str
    severity: str
    summary: str
    payload: dict[str, Any]
    redaction: dict[str, Any] | None
    usage: dict[str, Any] | None
    cost: dict[str, Any] | None
    schema_version: int


class EventPage(BaseModel):
    """Cursor page of events ordered by ``sequence``."""

    items: list[EventOut]
    next_after_sequence: int | None
    last_sequence: int
    total: int


class ArtifactOut(OrmModel):
    id: str
    run_id: str
    attempt_id: str | None
    step_run_id: str | None
    name: str
    kind: str
    media_type: str
    size_bytes: int
    sha256: str
    is_text: bool
    metadata: dict[str, Any] = Field(validation_alias="metadata_")
    created_at: datetime


class FileChangeOut(OrmModel):
    id: str
    run_id: str
    attempt_id: str | None
    path: str
    old_path: str | None
    change_type: str
    insertions: int | None
    deletions: int | None
    is_binary: bool
    size_bytes: int | None
    is_protected: bool
    diff_artifact_id: str | None
    created_at: datetime


class EvaluationResultOut(OrmModel):
    id: str
    run_id: str
    attempt_id: str | None
    step_run_id: str | None
    definition_id: str | None
    definition_version_id: str | None
    name: str
    evaluator_type: str
    evaluator_version: str
    status: str
    required: bool
    score: float | None
    evidence: dict[str, Any]
    explanation: str | None
    duration_ms: int | None
    created_at: datetime


class RunDetail(BaseModel):
    run: RunOut
    snapshot: dict[str, Any]
    inputs: dict[str, Any]
    rendered_prompt: str | None
    outputs: dict[str, Any] | None
    options: dict[str, Any]
    attempts: list[AttemptOut]
    steps: list[StepRunOut]
    artifacts: list[ArtifactOut]
    file_changes: list[FileChangeOut]
    evaluations: list[EvaluationResultOut]
    approvals: list[Any]
    children: list[RunOut]
    event_count: int
    last_sequence: int


class RunCancel(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


class RunRetry(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)
