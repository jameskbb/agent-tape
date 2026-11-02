"""DTOs for the prompt library operations, profile admin, secret references,
executable allowlist, project path checks and activity, and onboarding."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from agenttape.application.schemas.catalog import (
    AgentProfileOut,
    ProjectOut,
    PromptTemplateOut,
)
from agenttape.application.schemas.common import OrmModel
from agenttape.application.schemas.runs import RunOut
from agenttape.domain.prompts import VariableDefinition

# --------------------------------------------------------------------------- prompts


class PromptTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    tags: list[str] | None = Field(default=None, max_length=50)
    owner: str | None = Field(default=None, max_length=200)
    project_id: str | None = None
    archived: bool | None = None


class PromptDuplicateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    version_id: str | None = None


class PromptVersionAmend(BaseModel):
    """Correct a version that no run has used yet. Used versions are immutable."""

    body: str = Field(min_length=1, max_length=200_000)
    variables: list[VariableDefinition] = Field(default_factory=list)
    settings: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = None


class PromptVersionExport(BaseModel):
    version: int
    body: str
    variables: list[dict[str, Any]]
    settings: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = None
    content_hash: str | None = None
    created_at: datetime | None = None


class PromptTemplateExport(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    owner: str | None = None
    archived: bool = False
    versions: list[PromptVersionExport] = Field(min_length=1, max_length=500)


class PromptExportBundle(BaseModel):
    format: Literal["agenttape.prompts"] = "agenttape.prompts"
    format_version: int = 1
    exported_at: datetime | None = None
    templates: list[PromptTemplateExport] = Field(max_length=500)


class PromptImportResult(BaseModel):
    created: list[PromptTemplateOut]
    skipped: list[dict[str, str]]


class DiffLine(BaseModel):
    op: Literal["context", "add", "remove"]
    text: str
    old_no: int | None = None
    new_no: int | None = None


class PromptDiff(BaseModel):
    template_id: str
    from_version: int
    to_version: int
    unified: str
    lines: list[DiffLine]
    variables_added: list[str]
    variables_removed: list[str]
    variables_changed: list[str]
    body_changed: bool


class PromptTestRequest(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)
    version_id: str | None = None


class PromptTestResult(BaseModel):
    run: RunOut
    profile: AgentProfileOut


# --------------------------------------------------------------------------- profiles


class AgentProfileUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    tags: list[str] | None = None
    project_id: str | None = None
    archived: bool | None = None


class SecretReferenceCreate(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
    provider: Literal["env"] = "env"
    #: Environment variable to read (defaults to ``name``). Never the value itself.
    locator: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,254}$")
    description: str | None = Field(default=None, max_length=1000)


class SecretReferenceUpdate(BaseModel):
    locator: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,254}$")
    description: str | None = Field(default=None, max_length=1000)


class SecretReferenceOut(OrmModel):
    id: str
    name: str
    provider: str
    locator: str
    description: str | None
    last_resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime
    #: Whether the locator is currently set in the server's environment (value never shown).
    available: bool = False


class ExecutableEntry(BaseModel):
    path: str
    source: Literal["env", "settings"]
    exists: bool


class ExecutableAllowlist(BaseModel):
    entries: list[ExecutableEntry]
    env_var: str
    stored: list[str]


class ExecutableAllowlistUpdate(BaseModel):
    executables: list[str] = Field(default_factory=list, max_length=200)


class ExecutableCheckOut(BaseModel):
    requested: str
    path: str | None
    allowed: bool
    allowed_by: str | None
    error: str | None


# --------------------------------------------------------------------------- projects


class PathCheckRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class PathCheckResult(BaseModel):
    input: str
    canonical: str | None
    allowed: bool
    exists: bool
    is_dir: bool
    is_git_repo: bool
    matched_root: str | None
    allowed_roots: list[str]
    message: str
    git: dict[str, Any] | None = None


class ActivitySection(BaseModel):
    available: bool
    total: int | None = None
    items: list[dict[str, Any]] = Field(default_factory=list)
    note: str | None = None


class ProjectActivity(BaseModel):
    project: ProjectOut
    runs: list[RunOut]
    runs_total: int
    status_counts: dict[str, int]
    recent_failures: list[RunOut]
    profiles: list[AgentProfileOut]
    prompts: list[PromptTemplateOut]
    schedules: ActivitySection
    workflows: ActivitySection


# --------------------------------------------------------------------------- onboarding


class OnboardingState(BaseModel):
    show: bool
    dismissed: bool
    completed: bool
    has_user_project: bool
    demo_ready: bool
    updated_at: datetime | None = None


class OnboardingUpdate(BaseModel):
    action: Literal["dismiss", "complete", "reset"]
