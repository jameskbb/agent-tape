"""DTOs for schedules, the scheduler, webhook triggers, and limits."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from agenttape.application.schemas.common import OrmModel

ScheduleKind = Literal["one_time", "interval", "cron"]
TargetType = Literal["prompt", "workflow"]
MisfirePolicyName = Literal["skip", "run_once"]
ConcurrencyPolicyName = Literal["queue", "skip", "cancel_and_replace"]

# --------------------------------------------------------------------------- schedules


class ScheduleTiming(BaseModel):
    """When a schedule fires. Naive datetimes are local wall time in ``timezone``;
    datetimes with an offset are converted. Everything is stored in UTC."""

    kind: ScheduleKind
    timezone: str = Field(min_length=1, max_length=64)
    cron_expression: str | None = Field(default=None, max_length=200)
    interval_seconds: int | None = Field(default=None, ge=1)
    run_at: datetime | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None


class ScheduleCreate(ScheduleTiming):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    target_type: TargetType = "prompt"
    project_id: str | None = None
    prompt_template_id: str | None = None
    #: Pin a prompt version; omit to use the latest version at each occurrence.
    prompt_version_id: str | None = None
    agent_profile_id: str | None = None
    workflow_id: str | None = None
    workflow_version_id: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    misfire_policy: MisfirePolicyName = "run_once"
    concurrency_policy: ConcurrencyPolicyName = "queue"
    max_queued: int = Field(default=1, ge=0, le=100)
    priority: int = Field(default=0, ge=-100, le=100)


class ScheduleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    kind: ScheduleKind | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    cron_expression: str | None = Field(default=None, max_length=200)
    interval_seconds: int | None = Field(default=None, ge=1)
    run_at: datetime | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    clear_start_at: bool = False
    clear_end_at: bool = False
    target_type: TargetType | None = None
    project_id: str | None = None
    prompt_template_id: str | None = None
    prompt_version_id: str | None = None
    agent_profile_id: str | None = None
    workflow_id: str | None = None
    workflow_version_id: str | None = None
    inputs: dict[str, Any] | None = None
    enabled: bool | None = None
    misfire_policy: MisfirePolicyName | None = None
    concurrency_policy: ConcurrencyPolicyName | None = None
    max_queued: int | None = Field(default=None, ge=0, le=100)
    priority: int | None = Field(default=None, ge=-100, le=100)


class ScheduleOut(OrmModel):
    id: str
    workspace_id: str
    project_id: str | None
    name: str
    description: str | None
    target_type: str
    prompt_template_id: str | None
    prompt_version_id: str | None
    agent_profile_id: str | None
    workflow_id: str | None
    workflow_version_id: str | None
    inputs: dict[str, Any]
    kind: str
    cron_expression: str | None
    interval_seconds: int | None
    run_at: datetime | None
    timezone: str
    enabled: bool
    start_at: datetime | None
    end_at: datetime | None
    misfire_policy: str
    concurrency_policy: str
    max_queued: int
    priority: int
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_run_id: str | None
    is_demo: bool
    created_at: datetime
    updated_at: datetime
    #: Display fields (resolved by the service).
    target_name: str | None = None
    agent_profile_name: str | None = None
    project_name: str | None = None
    last_run_status: str | None = None
    active_runs: int = 0
    queued_occurrences: int = 0
    last_occurrence_status: str | None = None
    last_occurrence_reason: str | None = None
    #: Local wall-clock rendering of the key times in the schedule's zone.
    next_run_local: str | None = None
    run_at_local: str | None = None
    start_at_local: str | None = None
    end_at_local: str | None = None


class OccurrenceOut(OrmModel):
    id: str
    schedule_id: str
    occurrence_at: datetime
    status: str
    run_id: str | None
    reason: str | None
    created_at: datetime
    run_status: str | None = None
    occurrence_local: str | None = None


class PreviewItem(BaseModel):
    at: datetime
    local: str
    utc_offset: str
    note: str | None = None


class SchedulePreview(BaseModel):
    valid: bool
    error: str | None = None
    timezone: str
    occurrences: list[PreviewItem] = Field(default_factory=list)
    finished: bool = False


class SchedulePreviewRequest(ScheduleTiming):
    count: int = Field(default=5, ge=1, le=50)


class RunNowRequest(BaseModel):
    #: Ignore the concurrency policy (start even if a run of this schedule is active).
    force: bool = False


class RunNowResult(BaseModel):
    occurrence: OccurrenceOut
    run_id: str | None
    action: str
    message: str


class SchedulerStatusOut(BaseModel):
    #: running | standby | stopped | unknown
    state: str
    owner_id: str | None
    lease_expires_at: datetime | None
    last_tick_at: datetime | None
    instances: list[dict[str, Any]] = Field(default_factory=list)
    message: str


class TargetOption(BaseModel):
    id: str
    name: str
    description: str | None = None
    latest_version: int | None = None
    latest_version_id: str | None = None
    variables: list[dict[str, Any]] = Field(default_factory=list)
    adapter_type: str | None = None
    project_id: str | None = None
    is_demo: bool = False


class ScheduleTargets(BaseModel):
    prompts: list[TargetOption]
    agent_profiles: list[TargetOption]
    projects: list[TargetOption]
    workflows: list[TargetOption]
    #: False until a ``workflow`` run executor is installed.
    workflow_runs_available: bool


# --------------------------------------------------------------------------- triggers


class TriggerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    kind: str = Field(default="webhook", max_length=32)
    target_type: TargetType = "prompt"
    project_id: str | None = None
    prompt_template_id: str | None = None
    prompt_version_id: str | None = None
    agent_profile_id: str | None = None
    workflow_id: str | None = None
    #: ``{"variable": "$.json.path"}``; see docs/TRIGGERS.md for the path syntax.
    input_mapping: dict[str, str] = Field(default_factory=dict)
    static_inputs: dict[str, Any] = Field(default_factory=dict)
    #: JSON path to a delivery id in the payload (used when no header provides one).
    idempotency_key_path: str | None = Field(default=None, max_length=200)
    #: ``derive`` (header, delivery-id header, or body hash) or ``require`` (reject without one).
    idempotency: Literal["derive", "require"] = "derive"
    #: Accept the token as a ``?token=`` query parameter. Off by default: URLs are logged
    #: by senders, proxies, and servers. Enable only for senders that cannot set headers.
    allow_query_token: bool = False
    enabled: bool = True
    rate_limit_per_minute: int = Field(default=30, ge=1, le=6000)
    max_payload_bytes: int = Field(default=256 * 1024, ge=256, le=4 * 1024 * 1024)
    #: Provide your own token (>= 24 characters) or omit to generate one.
    token: str | None = Field(default=None, min_length=24, max_length=256)


class TriggerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    project_id: str | None = None
    prompt_template_id: str | None = None
    prompt_version_id: str | None = None
    agent_profile_id: str | None = None
    workflow_id: str | None = None
    input_mapping: dict[str, str] | None = None
    static_inputs: dict[str, Any] | None = None
    idempotency_key_path: str | None = Field(default=None, max_length=200)
    idempotency: Literal["derive", "require"] | None = None
    allow_query_token: bool | None = None
    enabled: bool | None = None
    rate_limit_per_minute: int | None = Field(default=None, ge=1, le=6000)
    max_payload_bytes: int | None = Field(default=None, ge=256, le=4 * 1024 * 1024)


class TriggerOut(OrmModel):
    id: str
    workspace_id: str
    project_id: str | None
    name: str
    description: str | None
    kind: str
    target_type: str
    prompt_template_id: str | None
    prompt_version_id: str | None
    agent_profile_id: str | None
    workflow_id: str | None
    token_prefix: str | None
    input_mapping: dict[str, Any]
    idempotency_key_path: str | None
    enabled: bool
    rate_limit_per_minute: int
    max_payload_bytes: int
    config: dict[str, Any]
    last_delivery_at: datetime | None
    is_demo: bool
    created_at: datetime
    updated_at: datetime
    webhook_path: str = ""
    target_name: str | None = None
    agent_profile_name: str | None = None
    delivery_count: int = 0


class TriggerWithToken(BaseModel):
    """Returned once when a trigger is created or its token rotated. Store the token now."""

    trigger: TriggerOut
    token: str


class DeliveryOut(OrmModel):
    id: str
    trigger_id: str
    received_at: datetime
    idempotency_key: str
    status: str
    http_status: int
    run_id: str | None
    payload_bytes: int
    metadata_: dict[str, Any] = Field(serialization_alias="metadata")
    error: str | None


class DeliveryResult(BaseModel):
    """Response body of ``POST /webhooks/{trigger_id}``."""

    status: str
    delivery_id: str | None
    run_id: str | None
    #: Machine-readable reason when the delivery was not accepted.
    code: str | None = None
    duplicate: bool = False
    idempotency_source: str | None = None
    message: str | None = None


class MappingTestRequest(BaseModel):
    payload: Any


class MappingTestResult(BaseModel):
    inputs: dict[str, Any]
    missing: list[str]
    errors: list[str]


# --------------------------------------------------------------------------- limits


class LimitsSettings(BaseModel):
    """Global limits (app setting ``limits``). ``None`` means no limit."""

    max_concurrent_runs: int | None = Field(default=None, ge=1, le=1000)
    #: ``{"demo": 2, "claude-code": 1}``
    per_adapter: dict[str, int] = Field(default_factory=dict)
    #: Default budget merged under every run's budget (project, profile, run override it).
    default_budget: dict[str, Any] = Field(default_factory=dict)


class LimitsOut(BaseModel):
    settings: LimitsSettings
    #: Budget keys with their meaning and unit (for the UI).
    budget_keys: list[dict[str, str]]
    active_runs: int
    active_by_adapter: dict[str, int]
    active_by_project: dict[str, int]
    worker_defaults: dict[str, Any]


class BudgetCheckOut(BaseModel):
    key: str
    label: str
    unit: str
    limit: float
    used: float | None
    ratio: float | None
    #: ok | warning | exceeded | not_applicable | unknown
    status: str
    note: str | None = None


class RunLimitsOut(BaseModel):
    run_id: str
    budget: dict[str, Any]
    budget_sources: dict[str, str]
    checks: list[BudgetCheckOut]
    timeout_seconds: float | None
    idle_timeout_seconds: float | None
    max_attempts: int
    attempt_count: int
    retry: dict[str, Any]
    concurrency: list[dict[str, Any]]
