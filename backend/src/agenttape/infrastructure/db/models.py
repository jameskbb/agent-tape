"""The complete AgentTape core data model.

Conventions
-----------
* Primary keys are opaque UUID strings (``String(36)``).
* All timestamps are timezone-aware UTC (see :class:`UTCDateTime`).
* Configuration and snapshots are JSON columns; a version row is immutable once
  ``locked_at`` is set (the first run that uses it locks it).
* ``is_demo`` tags seeded demo records. ``reset-demo`` deletes only ``is_demo`` rows.
* Deletion: children of an owned aggregate cascade (run -> attempts/events/artifacts);
  references from history to configuration are ``SET NULL`` because every run keeps an
  immutable snapshot of the configuration it used.
* Queries must stay PostgreSQL-compatible: no SQLite-only SQL.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from agenttape.infrastructure.db.base import Base, IdMixin, TimestampMixin, utcnow

ID = String(36)


def fk(target: str, ondelete: str, *, nullable: bool = True, index: bool = True) -> Any:
    return mapped_column(ID, ForeignKey(target, ondelete=ondelete), nullable=nullable, index=index)


# --------------------------------------------------------------------------- workspace


class Workspace(IdMixin, TimestampMixin, Base):
    __tablename__ = "workspaces"

    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class Project(IdMixin, TimestampMixin, Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    workspace_id: Mapped[str] = fk("workspaces.id", "CASCADE", nullable=False)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    #: Canonical absolute path; validated against the allowlisted roots on save.
    repo_path: Mapped[str] = mapped_column(Text)
    default_branch: Mapped[str | None] = mapped_column(String(255))
    tags: Mapped[list[Any]] = mapped_column(default=list)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    #: timeouts, concurrency, validation_commands, protected_paths, env_allowlist,
    #: artifact_rules, git_isolation, budgets.
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict)
    #: Last detected Git state (branch, head, dirty, worktree support) for display only.
    git_state: Mapped[dict[str, Any] | None] = mapped_column()
    git_checked_at: Mapped[datetime | None] = mapped_column()
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


# --------------------------------------------------------------------------- agent profiles


class AgentProfile(IdMixin, TimestampMixin, Base):
    __tablename__ = "agent_profiles"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    workspace_id: Mapped[str] = fk("workspaces.id", "CASCADE", nullable=False)
    project_id: Mapped[str | None] = fk("projects.id", "SET NULL")
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    adapter_type: Mapped[str] = mapped_column(String(64), index=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    latest_version: Mapped[int] = mapped_column(Integer, default=0)
    tags: Mapped[list[Any]] = mapped_column(default=list)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class AgentProfileVersion(IdMixin, Base):
    __tablename__ = "agent_profile_versions"
    __table_args__ = (UniqueConstraint("profile_id", "version"),)

    profile_id: Mapped[str] = fk("agent_profiles.id", "CASCADE", nullable=False)
    version: Mapped[int] = mapped_column(Integer)
    adapter_type: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    #: executable, args, model, workdir_mode, env_allowlist, secret_refs, timeout_seconds,
    #: idle_timeout_seconds, max_attempts, retry, permissions, allowed_tools, denied_tools,
    #: budget, parser, preflight_commands, validation_commands, evaluators, adapter options.
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    config_hash: Mapped[str] = mapped_column(String(64))
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    locked_at: Mapped[datetime | None] = mapped_column()


# --------------------------------------------------------------------------- prompts


class PromptTemplate(IdMixin, TimestampMixin, Base):
    __tablename__ = "prompt_templates"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    workspace_id: Mapped[str] = fk("workspaces.id", "CASCADE", nullable=False)
    project_id: Mapped[str | None] = fk("projects.id", "SET NULL")
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[Any]] = mapped_column(default=list)
    owner: Mapped[str | None] = mapped_column(String(200))
    archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    latest_version: Mapped[int] = mapped_column(Integer, default=0)
    duplicated_from_id: Mapped[str | None] = fk("prompt_templates.id", "SET NULL")
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class PromptVersion(IdMixin, Base):
    __tablename__ = "prompt_versions"
    __table_args__ = (UniqueConstraint("template_id", "version"),)

    template_id: Mapped[str] = fk("prompt_templates.id", "CASCADE", nullable=False)
    version: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text)
    #: list of VariableDefinition dicts
    variables: Mapped[list[Any]] = mapped_column(default=list)
    #: Optional default agent profile and output expectations.
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict)
    content_hash: Mapped[str] = mapped_column(String(64))
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    #: Set when first used by a run; locked versions are immutable.
    locked_at: Mapped[datetime | None] = mapped_column()


# --------------------------------------------------------------------------- workflows


class Workflow(IdMixin, TimestampMixin, Base):
    __tablename__ = "workflows"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    workspace_id: Mapped[str] = fk("workspaces.id", "CASCADE", nullable=False)
    project_id: Mapped[str | None] = fk("projects.id", "SET NULL")
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[Any]] = mapped_column(default=list)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    latest_version: Mapped[int] = mapped_column(Integer, default=0)
    max_concurrency: Mapped[int | None] = mapped_column(Integer)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class WorkflowVersion(IdMixin, Base):
    __tablename__ = "workflow_versions"
    __table_args__ = (UniqueConstraint("workflow_id", "version"),)

    workflow_id: Mapped[str] = fk("workflows.id", "CASCADE", nullable=False)
    version: Mapped[int] = mapped_column(Integer)
    #: Full definition snapshot (nodes, edges, inputs schema, outputs, settings).
    definition: Mapped[dict[str, Any]] = mapped_column(default=dict)
    input_schema: Mapped[dict[str, Any] | None] = mapped_column()
    content_hash: Mapped[str] = mapped_column(String(64))
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    locked_at: Mapped[datetime | None] = mapped_column()


class WorkflowNode(IdMixin, Base):
    __tablename__ = "workflow_nodes"
    __table_args__ = (UniqueConstraint("workflow_version_id", "node_key"),)

    workflow_version_id: Mapped[str] = fk("workflow_versions.id", "CASCADE", nullable=False)
    node_key: Mapped[str] = mapped_column(String(100))
    #: agent_run | command | transform | evaluation_gate | approval | condition | collect
    node_type: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    input_schema: Mapped[dict[str, Any] | None] = mapped_column()
    output_schema: Mapped[dict[str, Any] | None] = mapped_column()
    timeout_seconds: Mapped[float | None] = mapped_column(Float)
    max_attempts: Mapped[int] = mapped_column(Integer, default=1)
    retry: Mapped[dict[str, Any] | None] = mapped_column()
    continue_on_failure: Mapped[bool] = mapped_column(Boolean, default=False)
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    position: Mapped[dict[str, Any] | None] = mapped_column()


class WorkflowEdge(IdMixin, Base):
    __tablename__ = "workflow_edges"
    __table_args__ = (UniqueConstraint("workflow_version_id", "from_node_key", "to_node_key"),)

    workflow_version_id: Mapped[str] = fk("workflow_versions.id", "CASCADE", nullable=False)
    from_node_key: Mapped[str] = mapped_column(String(100))
    to_node_key: Mapped[str] = mapped_column(String(100))
    #: Optional condition on the upstream result (status, evaluation, output path).
    condition: Mapped[dict[str, Any] | None] = mapped_column()


# --------------------------------------------------------------------------- scheduling


class Schedule(IdMixin, TimestampMixin, Base):
    __tablename__ = "schedules"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name"),
        Index("ix_schedules_enabled_next_run_at", "enabled", "next_run_at"),
    )

    workspace_id: Mapped[str] = fk("workspaces.id", "CASCADE", nullable=False)
    project_id: Mapped[str | None] = fk("projects.id", "SET NULL")
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    #: prompt | workflow
    target_type: Mapped[str] = mapped_column(String(32))
    prompt_template_id: Mapped[str | None] = fk("prompt_templates.id", "SET NULL")
    #: Pinned version; NULL means "latest version at occurrence time".
    prompt_version_id: Mapped[str | None] = fk("prompt_versions.id", "SET NULL")
    agent_profile_id: Mapped[str | None] = fk("agent_profiles.id", "SET NULL")
    workflow_id: Mapped[str | None] = fk("workflows.id", "SET NULL")
    workflow_version_id: Mapped[str | None] = fk("workflow_versions.id", "SET NULL")
    inputs: Mapped[dict[str, Any]] = mapped_column(default=dict)
    #: one_time | interval | cron
    kind: Mapped[str] = mapped_column(String(16))
    cron_expression: Mapped[str | None] = mapped_column(String(200))
    interval_seconds: Mapped[int | None] = mapped_column(Integer)
    run_at: Mapped[datetime | None] = mapped_column()
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    start_at: Mapped[datetime | None] = mapped_column()
    end_at: Mapped[datetime | None] = mapped_column()
    #: skip | run_once
    misfire_policy: Mapped[str] = mapped_column(String(16), default="run_once")
    #: queue | skip | cancel_and_replace
    concurrency_policy: Mapped[str] = mapped_column(String(32), default="queue")
    max_queued: Mapped[int] = mapped_column(Integer, default=1)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    next_run_at: Mapped[datetime | None] = mapped_column()
    last_run_at: Mapped[datetime | None] = mapped_column()
    last_run_id: Mapped[str | None] = mapped_column(ID)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class ScheduleOccurrence(IdMixin, Base):
    """One due time of a schedule. The unique key makes occurrence creation idempotent."""

    __tablename__ = "schedule_occurrences"
    __table_args__ = (UniqueConstraint("schedule_id", "occurrence_at"),)

    schedule_id: Mapped[str] = fk("schedules.id", "CASCADE", nullable=False)
    occurrence_at: Mapped[datetime] = mapped_column()
    #: created | skipped | missed | replaced | failed
    status: Mapped[str] = mapped_column(String(32))
    run_id: Mapped[str | None] = fk("runs.id", "SET NULL")
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Trigger(IdMixin, TimestampMixin, Base):
    __tablename__ = "triggers"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    workspace_id: Mapped[str] = fk("workspaces.id", "CASCADE", nullable=False)
    project_id: Mapped[str | None] = fk("projects.id", "SET NULL")
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    #: webhook (future: jira, github_app)
    kind: Mapped[str] = mapped_column(String(32), default="webhook")
    target_type: Mapped[str] = mapped_column(String(32))
    prompt_template_id: Mapped[str | None] = fk("prompt_templates.id", "SET NULL")
    prompt_version_id: Mapped[str | None] = fk("prompt_versions.id", "SET NULL")
    agent_profile_id: Mapped[str | None] = fk("agent_profiles.id", "SET NULL")
    workflow_id: Mapped[str | None] = fk("workflows.id", "SET NULL")
    token_hash: Mapped[str | None] = mapped_column(String(128))
    token_prefix: Mapped[str | None] = mapped_column(String(32))
    #: {"variable": "$.json.path"} mapping from payload to run inputs
    input_mapping: Mapped[dict[str, Any]] = mapped_column(default=dict)
    idempotency_key_path: Mapped[str | None] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, default=30)
    max_payload_bytes: Mapped[int] = mapped_column(Integer, default=256 * 1024)
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    last_delivery_at: Mapped[datetime | None] = mapped_column()
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class TriggerDelivery(IdMixin, Base):
    __tablename__ = "trigger_deliveries"
    __table_args__ = (UniqueConstraint("trigger_id", "idempotency_key"),)

    trigger_id: Mapped[str] = fk("triggers.id", "CASCADE", nullable=False)
    received_at: Mapped[datetime] = mapped_column(default=utcnow)
    idempotency_key: Mapped[str] = mapped_column(String(255))
    #: accepted | duplicate | rejected | error
    status: Mapped[str] = mapped_column(String(32))
    http_status: Mapped[int] = mapped_column(Integer)
    run_id: Mapped[str | None] = fk("runs.id", "SET NULL")
    payload_bytes: Mapped[int] = mapped_column(Integer, default=0)
    #: Redacted, size-capped summary of the payload and headers.
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", default=dict)
    error: Mapped[str | None] = mapped_column(Text)


# --------------------------------------------------------------------------- runs


class Run(IdMixin, TimestampMixin, Base):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("source", "idempotency_key"),
        Index("ix_runs_status_created_at", "status", "created_at"),
        Index("ix_runs_created_at", "created_at"),
    )

    workspace_id: Mapped[str] = fk("workspaces.id", "CASCADE", nullable=False)
    project_id: Mapped[str | None] = fk("projects.id", "SET NULL")
    title: Mapped[str] = mapped_column(String(300))
    #: Executor that runs it: ``agent`` (single adapter execution) or ``workflow`` (DAG).
    kind: Mapped[str] = mapped_column(String(32), default="agent", index=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    #: Free-form reference to the originating object (schedule occurrence, delivery, node).
    source_ref: Mapped[str | None] = mapped_column(String(255))
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), index=True)
    failure_category: Mapped[str | None] = mapped_column(String(64), index=True)
    failure_message: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer, default=0)

    adapter_type: Mapped[str | None] = mapped_column(String(64), index=True)
    prompt_template_id: Mapped[str | None] = fk("prompt_templates.id", "SET NULL")
    prompt_version_id: Mapped[str | None] = fk("prompt_versions.id", "SET NULL")
    agent_profile_id: Mapped[str | None] = fk("agent_profiles.id", "SET NULL")
    agent_profile_version_id: Mapped[str | None] = fk("agent_profile_versions.id", "SET NULL")
    workflow_id: Mapped[str | None] = fk("workflows.id", "SET NULL")
    workflow_version_id: Mapped[str | None] = fk("workflow_versions.id", "SET NULL")
    schedule_id: Mapped[str | None] = fk("schedules.id", "SET NULL")
    trigger_id: Mapped[str | None] = fk("triggers.id", "SET NULL")

    #: replay / fork / workflow parent
    parent_run_id: Mapped[str | None] = fk("runs.id", "SET NULL")
    #: replay | fork | workflow_node
    parent_relation: Mapped[str | None] = mapped_column(String(32))
    parent_node_key: Mapped[str | None] = mapped_column(String(100))

    inputs: Mapped[dict[str, Any]] = mapped_column(default=dict)
    #: Rendered prompt with secret markers only (never secret values).
    rendered_prompt: Mapped[str | None] = mapped_column(Text)
    #: Immutable snapshot of everything that determined this run (see RunService).
    snapshot: Mapped[dict[str, Any]] = mapped_column(default=dict)
    snapshot_hash: Mapped[str | None] = mapped_column(String(64))
    #: Per-run evaluator configuration and limits.
    options: Mapped[dict[str, Any]] = mapped_column(default=dict)
    outputs: Mapped[dict[str, Any] | None] = mapped_column()
    tags: Mapped[list[Any]] = mapped_column(default=list)
    notes: Mapped[str | None] = mapped_column(Text)

    max_attempts: Mapped[int] = mapped_column(Integer, default=1)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    #: Monotonic event sequence allocator (``UPDATE ... SET event_seq = event_seq + n``).
    event_seq: Mapped[int] = mapped_column(Integer, default=0)
    #: Increments each time the run is placed on the queue (dedupes queue items).
    queue_generation: Mapped[int] = mapped_column(Integer, default=0)

    queued_at: Mapped[datetime | None] = mapped_column()
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    cancel_requested_at: Mapped[datetime | None] = mapped_column()
    cancel_reason: Mapped[str | None] = mapped_column(Text)

    #: Aggregated usage as reported by adapters (``simulated`` flag preserved); NULL = unknown.
    usage: Mapped[dict[str, Any] | None] = mapped_column()
    cost: Mapped[dict[str, Any] | None] = mapped_column()
    budget: Mapped[dict[str, Any] | None] = mapped_column()
    created_by: Mapped[str | None] = mapped_column(String(200))
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class RunAttempt(IdMixin, Base):
    __tablename__ = "run_attempts"
    __table_args__ = (
        UniqueConstraint("run_id", "attempt_number"),
        Index("ix_run_attempts_status_lease", "status", "lease_expires_at"),
    )

    run_id: Mapped[str] = fk("runs.id", "CASCADE", nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32))
    failure_category: Mapped[str | None] = mapped_column(String(64))
    failure_message: Mapped[str | None] = mapped_column(Text)
    worker_id: Mapped[str | None] = mapped_column(String(200))
    #: Informational only: never used to signal a process after a restart.
    pid: Mapped[int | None] = mapped_column(Integer)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    workdir: Mapped[str | None] = mapped_column(Text)
    log_dir: Mapped[str | None] = mapped_column(Text)
    stdout_bytes: Mapped[int] = mapped_column(Integer, default=0)
    stderr_bytes: Mapped[int] = mapped_column(Integer, default=0)
    #: Adapter checkpoint used to resume after an approval without redoing work.
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column()
    outputs: Mapped[dict[str, Any] | None] = mapped_column()
    usage: Mapped[dict[str, Any] | None] = mapped_column()
    cost: Mapped[dict[str, Any] | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    heartbeat_at: Mapped[datetime | None] = mapped_column()
    lease_expires_at: Mapped[datetime | None] = mapped_column()
    retry_scheduled_at: Mapped[datetime | None] = mapped_column()


class StepRun(IdMixin, TimestampMixin, Base):
    __tablename__ = "step_runs"
    __table_args__ = (UniqueConstraint("run_id", "node_key"),)

    run_id: Mapped[str] = fk("runs.id", "CASCADE", nullable=False)
    node_key: Mapped[str] = mapped_column(String(100))
    node_type: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    child_run_id: Mapped[str | None] = fk("runs.id", "SET NULL")
    approval_request_id: Mapped[str | None] = mapped_column(ID)
    inputs: Mapped[dict[str, Any] | None] = mapped_column()
    outputs: Mapped[dict[str, Any] | None] = mapped_column()
    state: Mapped[dict[str, Any] | None] = mapped_column()
    failure_category: Mapped[str | None] = mapped_column(String(64))
    failure_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()
    duration_ms: Mapped[int | None] = mapped_column(Integer)


class Event(IdMixin, Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence"),
        Index("ix_events_run_id_type", "run_id", "type"),
    )

    run_id: Mapped[str] = fk("runs.id", "CASCADE", nullable=False, index=False)
    attempt_id: Mapped[str | None] = fk("run_attempts.id", "CASCADE")
    step_run_id: Mapped[str | None] = fk("step_runs.id", "SET NULL")
    node_key: Mapped[str | None] = mapped_column(String(100))
    parent_event_id: Mapped[str | None] = mapped_column(ID)
    sequence: Mapped[int] = mapped_column(Integer)
    ts: Mapped[datetime] = mapped_column()
    #: Milliseconds since the run's first event (monotonic clock within a process).
    monotonic_ms: Mapped[float] = mapped_column(Float, default=0.0)
    type: Mapped[str] = mapped_column(String(48))
    source: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(16), index=True)
    summary: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    redaction: Mapped[dict[str, Any] | None] = mapped_column()
    usage: Mapped[dict[str, Any] | None] = mapped_column()
    cost: Mapped[dict[str, Any] | None] = mapped_column()
    schema_version: Mapped[int] = mapped_column(Integer, default=1)


class Artifact(IdMixin, Base):
    __tablename__ = "artifacts"

    run_id: Mapped[str] = fk("runs.id", "CASCADE", nullable=False)
    attempt_id: Mapped[str | None] = fk("run_attempts.id", "SET NULL")
    step_run_id: Mapped[str | None] = fk("step_runs.id", "SET NULL")
    name: Mapped[str] = mapped_column(String(255))
    #: log | report | diff | output | file | bundle
    kind: Mapped[str] = mapped_column(String(32), default="file")
    media_type: Mapped[str] = mapped_column(String(128), default="application/octet-stream")
    #: Path relative to the artifact store root.
    storage_path: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    is_text: Mapped[bool] = mapped_column(Boolean, default=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class FileChange(IdMixin, Base):
    __tablename__ = "file_changes"

    run_id: Mapped[str] = fk("runs.id", "CASCADE", nullable=False)
    attempt_id: Mapped[str | None] = fk("run_attempts.id", "SET NULL")
    step_run_id: Mapped[str | None] = fk("step_runs.id", "SET NULL")
    path: Mapped[str] = mapped_column(Text)
    old_path: Mapped[str | None] = mapped_column(Text)
    change_type: Mapped[str] = mapped_column(String(16))
    insertions: Mapped[int | None] = mapped_column(Integer)
    deletions: Mapped[int | None] = mapped_column(Integer)
    is_binary: Mapped[bool] = mapped_column(Boolean, default=False)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    is_protected: Mapped[bool] = mapped_column(Boolean, default=False)
    diff_artifact_id: Mapped[str | None] = fk("artifacts.id", "SET NULL")
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


# --------------------------------------------------------------------------- evaluations


class EvaluationDefinition(IdMixin, TimestampMixin, Base):
    __tablename__ = "evaluation_definitions"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    workspace_id: Mapped[str] = fk("workspaces.id", "CASCADE", nullable=False)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    evaluator_type: Mapped[str] = mapped_column(String(64))
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    latest_version: Mapped[int] = mapped_column(Integer, default=0)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class EvaluationDefinitionVersion(IdMixin, Base):
    __tablename__ = "evaluation_definition_versions"
    __table_args__ = (UniqueConstraint("definition_id", "version"),)

    definition_id: Mapped[str] = fk("evaluation_definitions.id", "CASCADE", nullable=False)
    version: Mapped[int] = mapped_column(Integer)
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    locked_at: Mapped[datetime | None] = mapped_column()


class EvaluationSuite(IdMixin, TimestampMixin, Base):
    __tablename__ = "evaluation_suites"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    workspace_id: Mapped[str] = fk("workspaces.id", "CASCADE", nullable=False)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    #: [{"definition_id", "version" | null, "required": bool}]
    members: Mapped[list[Any]] = mapped_column(default=list)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class EvaluationResult(IdMixin, Base):
    __tablename__ = "evaluation_results"

    run_id: Mapped[str] = fk("runs.id", "CASCADE", nullable=False)
    attempt_id: Mapped[str | None] = fk("run_attempts.id", "SET NULL")
    step_run_id: Mapped[str | None] = fk("step_runs.id", "SET NULL")
    definition_id: Mapped[str | None] = fk("evaluation_definitions.id", "SET NULL")
    definition_version_id: Mapped[str | None] = fk("evaluation_definition_versions.id", "SET NULL")
    suite_id: Mapped[str | None] = fk("evaluation_suites.id", "SET NULL")
    name: Mapped[str] = mapped_column(String(200))
    evaluator_type: Mapped[str] = mapped_column(String(64))
    evaluator_version: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), index=True)
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    score: Mapped[float | None] = mapped_column(Float)
    evidence: Mapped[dict[str, Any]] = mapped_column(default=dict)
    explanation: Mapped[str | None] = mapped_column(Text)
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    rerun_of_id: Mapped[str | None] = mapped_column(ID)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


# --------------------------------------------------------------------------- approvals & audit


class ApprovalRequest(IdMixin, TimestampMixin, Base):
    __tablename__ = "approval_requests"
    __table_args__ = (Index("ix_approval_requests_status_expires_at", "status", "expires_at"),)

    workspace_id: Mapped[str] = fk("workspaces.id", "CASCADE", nullable=False)
    run_id: Mapped[str | None] = fk("runs.id", "CASCADE")
    attempt_id: Mapped[str | None] = fk("run_attempts.id", "SET NULL")
    step_run_id: Mapped[str | None] = fk("step_runs.id", "SET NULL")
    node_key: Mapped[str | None] = mapped_column(String(100))
    #: run_continue | workflow_node | git_commit | elevated_command | protected_path |
    #: budget_increase | evaluation_override
    type: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(300))
    rationale: Mapped[str | None] = mapped_column(Text)
    risk_level: Mapped[str] = mapped_column(String(16), default="medium")
    requested_action: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(16), index=True)
    #: Registered resolution handler name (see ApprovalService.register_handler).
    resume_handler: Mapped[str] = mapped_column(String(64), default="run")
    requester: Mapped[str] = mapped_column(String(200), default="agenttape")
    reviewer: Mapped[str | None] = mapped_column(String(200))
    reviewer_note: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column()
    resolved_at: Mapped[datetime | None] = mapped_column()
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class AuditEntry(IdMixin, Base):
    """Append-only record of material actions. Never updated or deleted by the app."""

    __tablename__ = "audit_entries"
    __table_args__ = (
        Index("ix_audit_entries_resource", "resource_type", "resource_id"),
        Index("ix_audit_entries_ts", "ts"),
    )

    workspace_id: Mapped[str | None] = fk("workspaces.id", "SET NULL")
    ts: Mapped[datetime] = mapped_column(default=utcnow)
    actor: Mapped[str] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(100), index=True)
    resource_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(ID)
    run_id: Mapped[str | None] = mapped_column(ID, index=True)
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)


class SecretReference(IdMixin, TimestampMixin, Base):
    """Metadata only: where a secret can be resolved. The value is never stored."""

    __tablename__ = "secret_references"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    workspace_id: Mapped[str] = fk("workspaces.id", "CASCADE", nullable=False)
    name: Mapped[str] = mapped_column(String(128))
    #: env (pluggable)
    provider: Mapped[str] = mapped_column(String(32), default="env")
    locator: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    last_resolved_at: Mapped[datetime | None] = mapped_column()


# --------------------------------------------------------------------------- runtime infra


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    hostname: Mapped[str] = mapped_column(String(255))
    pid: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32), default="worker")
    version: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))
    capacity: Mapped[int] = mapped_column(Integer, default=1)
    active_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column()
    last_heartbeat_at: Mapped[datetime] = mapped_column(index=True)
    stopped_at: Mapped[datetime | None] = mapped_column()
    info: Mapped[dict[str, Any]] = mapped_column(default=dict)


class QueueItem(IdMixin, Base):
    """Database-backed queue with leases (see ADR 0003)."""

    __tablename__ = "queue_items"
    __table_args__ = (
        Index("ix_queue_items_claim", "status", "priority", "available_at", "enqueued_at"),
        Index("ix_queue_items_lease", "status", "lease_expires_at"),
    )

    #: Unique per enqueue intent (e.g. ``run:<id>:<generation>``) so duplicates cannot exist.
    dedupe_key: Mapped[str] = mapped_column(String(255), unique=True)
    kind: Mapped[str] = mapped_column(String(32), default="run")
    run_id: Mapped[str | None] = fk("runs.id", "CASCADE")
    queue_name: Mapped[str] = mapped_column(String(64), default="default")
    priority: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    enqueued_at: Mapped[datetime] = mapped_column(default=utcnow)
    available_at: Mapped[datetime] = mapped_column(default=utcnow)
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column()
    claim_count: Mapped[int] = mapped_column(Integer, default=0)
    last_claimed_at: Mapped[datetime | None] = mapped_column()
    completed_at: Mapped[datetime | None] = mapped_column()


class SchedulerLease(Base):
    """Named singleton leases (``scheduler`` and any other single-owner loop)."""

    __tablename__ = "scheduler_leases"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(200))
    acquired_at: Mapped[datetime] = mapped_column()
    renewed_at: Mapped[datetime] = mapped_column()
    expires_at: Mapped[datetime] = mapped_column()


class AppSetting(Base):
    """Key/value settings editable at runtime (allowed roots, retention, limits)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(default=dict)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


# Tables added after the core model live in their own modules (see extra_models/__init__).
from agenttape.plugins import import_submodules  # noqa: E402

import_submodules("agenttape.infrastructure.db.extra_models")
