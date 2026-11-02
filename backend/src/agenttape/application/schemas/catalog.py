"""DTOs for workspaces, projects, prompts, agent profiles, approvals, and system info."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from agenttape.application.schemas.common import OrmModel
from agenttape.domain.policy import Verdict, check_command, forbidden_env_name
from agenttape.domain.prompts import VariableDefinition

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# --------------------------------------------------------------------------- workspace/project


class WorkspaceOut(OrmModel):
    id: str
    name: str
    slug: str
    description: str | None
    is_default: bool
    is_demo: bool
    created_at: datetime


class ProjectSettings(BaseModel):
    """Project-level execution settings. Unknown keys are preserved for later versions."""

    model_config = {"extra": "allow"}

    timeout_seconds: float | None = Field(default=None, gt=0)
    idle_timeout_seconds: float | None = Field(default=None, gt=0)
    max_concurrent_runs: int | None = Field(default=None, ge=1)
    validation_commands: list[list[str]] = Field(default_factory=list)
    protected_paths: list[str] = Field(default_factory=list)
    env_allowlist: list[str] = Field(default_factory=list)
    artifact_rules: dict[str, Any] = Field(default_factory=dict)
    git_isolation: bool = False

    @field_validator("env_allowlist")
    @classmethod
    def _env_names(cls, value: list[str]) -> list[str]:
        for name in value:
            if not _ENV_NAME.match(name):
                raise ValueError(f"'{name}' is not an environment variable name")
            if forbidden_env_name(name):
                raise ValueError(
                    f"'{name}' cannot be passed to commands: it changes what git or the "
                    "dynamic loader runs without appearing in the command"
                )
        return value

    @field_validator("validation_commands")
    @classmethod
    def _validation_policy(cls, value: list[list[str]]) -> list[list[str]]:
        for argv in value:
            if not argv:
                continue
            decision = check_command(argv, trusted_commands=[argv])
            if decision.verdict == Verdict.DENY:
                raise ValueError(
                    f"Validation command {' '.join(argv)[:120]!r} is refused by the V1 "
                    f"safety policy: {decision.reason}"
                )
        return value


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    repo_path: str = Field(min_length=1)
    description: str | None = None
    default_branch: str | None = None
    tags: list[str] = Field(default_factory=list)
    settings: ProjectSettings = Field(default_factory=ProjectSettings)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    default_branch: str | None = None
    tags: list[str] | None = None
    settings: ProjectSettings | None = None
    archived: bool | None = None


class ProjectOut(OrmModel):
    id: str
    workspace_id: str
    name: str
    description: str | None
    repo_path: str
    default_branch: str | None
    tags: list[str]
    archived: bool
    settings: dict[str, Any]
    git_state: dict[str, Any] | None
    git_checked_at: datetime | None
    is_demo: bool
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------- prompts


class PromptTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    owner: str | None = None
    project_id: str | None = None
    body: str = Field(min_length=1, max_length=200_000)
    variables: list[VariableDefinition] = Field(default_factory=list)
    settings: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = None


class PromptVersionCreate(BaseModel):
    body: str = Field(min_length=1, max_length=200_000)
    variables: list[VariableDefinition] = Field(default_factory=list)
    settings: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = None


class PromptVersionOut(OrmModel):
    id: str
    template_id: str
    version: int
    body: str
    variables: list[dict[str, Any]]
    settings: dict[str, Any]
    content_hash: str
    notes: str | None
    created_by: str | None
    created_at: datetime
    locked_at: datetime | None


class PromptTemplateOut(OrmModel):
    id: str
    workspace_id: str
    project_id: str | None
    name: str
    description: str | None
    tags: list[str]
    owner: str | None
    archived: bool
    latest_version: int
    is_demo: bool
    created_at: datetime
    updated_at: datetime
    latest: PromptVersionOut | None = None


class RenderRequest(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)
    version_id: str | None = None


class RenderPreview(BaseModel):
    text: str
    secret_refs: list[str]
    issues: list[dict[str, str]]
    valid: bool


# --------------------------------------------------------------------------- agent profiles


class AgentProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    adapter_type: str = Field(min_length=1, max_length=64)
    description: str | None = None
    project_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = None


class AgentProfileVersionCreate(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)
    description: str | None = None
    notes: str | None = None


class AgentProfileVersionOut(OrmModel):
    id: str
    profile_id: str
    version: int
    adapter_type: str
    display_name: str
    description: str | None
    config: dict[str, Any]
    config_hash: str
    notes: str | None
    created_by: str | None
    created_at: datetime
    locked_at: datetime | None


class AgentProfileOut(OrmModel):
    id: str
    workspace_id: str
    project_id: str | None
    name: str
    description: str | None
    adapter_type: str
    archived: bool
    latest_version: int
    tags: list[str]
    is_demo: bool
    created_at: datetime
    updated_at: datetime
    latest: AgentProfileVersionOut | None = None


class ValidationIssueOut(BaseModel):
    field: str
    message: str
    severity: str = "error"


class AdapterInfo(BaseModel):
    name: str
    display_name: str
    description: str
    capabilities: list[str]
    #: How this adapter's runs record findings ("mcp", "scripted", "unsupported", or
    #: "per_profile" when it depends on the profile's agent, as with Herdr; each run then
    #: carries its own ``RunOut.findings_capture``). The Findings tab's empty state reads
    #: this instead of a frontend adapter-name lookup.
    findings: Literal["mcp", "scripted", "unsupported", "per_profile"] = "unsupported"
    status: str
    version: str | None
    executable: str | None
    message: str | None
    details: dict[str, Any] = Field(default_factory=dict)
    config_schema: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- approvals


class ApprovalOut(OrmModel):
    id: str
    workspace_id: str
    run_id: str | None
    attempt_id: str | None
    step_run_id: str | None
    node_key: str | None
    type: str
    title: str
    rationale: str | None
    risk_level: str
    requested_action: dict[str, Any]
    status: str
    resume_handler: str
    requester: str
    reviewer: str | None
    reviewer_note: str | None
    expires_at: datetime | None
    resolved_at: datetime | None
    is_demo: bool
    created_at: datetime
    updated_at: datetime


class ApprovalDecision(BaseModel):
    reviewer: str = Field(default="local user", min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=5000)


# --------------------------------------------------------------------------- system


class HealthOut(BaseModel):
    status: str
    version: str
    time: datetime


class ReadyCheck(BaseModel):
    name: str
    ok: bool
    detail: str | None = None


class ReadyOut(BaseModel):
    status: str
    checks: list[ReadyCheck]


class AuditEntryOut(OrmModel):
    id: str
    workspace_id: str | None
    ts: datetime
    actor: str
    action: str
    resource_type: str
    resource_id: str | None
    run_id: str | None
    details: dict[str, Any]
