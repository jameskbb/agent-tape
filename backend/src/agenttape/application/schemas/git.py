"""DTOs for Git isolation, file changes, commits, replay/fork, comparison, retention."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from agenttape.application.schemas.catalog import ApprovalOut
from agenttape.application.schemas.runs import EvaluationResultOut, RunCreateResult, RunOut

# --------------------------------------------------------------------------- git view


class WorkspaceOut(BaseModel):
    run_id: str
    isolated: bool
    owned: bool
    status: str
    path: str | None
    repo_path: str | None
    branch: str | None
    base_commit: str | None
    adapter_type: str | None
    adapter_version: str | None
    last_commit_sha: str | None
    commits: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime
    removed_at: datetime | None
    #: Live check of the worktree (None when it is not an active isolated worktree).
    uncommitted_count: int | None = None
    exists: bool = False


class StartingRevision(BaseModel):
    is_repo: bool
    head: str | None = None
    branch: str | None = None
    dirty: bool = False
    dirty_count: int = 0
    dirty_files: list[str] = Field(default_factory=list)
    error: str | None = None


class DiffLineOut(BaseModel):
    kind: str
    text: str
    old: int | None = None
    new: int | None = None


class DiffHunkOut(BaseModel):
    header: str
    section: str = ""
    lines: list[DiffLineOut]


class ChangedFileOut(BaseModel):
    id: str
    path: str
    old_path: str | None
    change_type: str
    insertions: int | None
    deletions: int | None
    is_binary: bool
    size_bytes: int | None
    is_protected: bool
    protected_patterns: list[str] = Field(default_factory=list)
    pre_existing_dirty: bool = False
    attempt_id: str | None
    hunks: list[DiffHunkOut] = Field(default_factory=list)
    diff_available: bool = False
    diff_truncated: bool = False
    #: Why an untracked file has no hunk, when AgentTape left it out on purpose:
    #: ``size_cap`` (the file is over the per-file cap) or ``count_cap`` (the run created
    #: more untracked files than AgentTape diffs).
    diff_omitted: Literal["size_cap", "count_cap"] | None = None


class ChangeSummary(BaseModel):
    files: int
    insertions: int
    deletions: int
    binary_files: int
    protected_files: int
    added: int
    modified: int
    deleted: int
    renamed: int


class CommitState(BaseModel):
    available: bool
    reason: str | None = None
    suggested_message: str
    pending: ApprovalOut | None = None
    history: list[ApprovalOut] = Field(default_factory=list)


class RunGitView(BaseModel):
    run_id: str
    project_name: str | None
    repo_path: str | None
    git_isolation: bool
    protected_patterns: list[str]
    validation_commands: list[list[str]]
    starting: StartingRevision | None
    workspace: WorkspaceOut | None
    summary: ChangeSummary
    files: list[ChangedFileOut]
    attempt_number: int | None
    diff_artifact_id: str | None
    diff_truncated: bool
    validations: list[EvaluationResultOut]
    commit: CommitState


class CommitRequest(BaseModel):
    message: str = Field(min_length=1, max_length=5000)


class CleanupOut(BaseModel):
    removed: bool
    message: str
    path: str
    branch: str | None = None
    branch_deleted: bool = False
    branch_kept_reason: str | None = None
    uncommitted: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- lineage


class ReplayRequest(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    notes: str | None = Field(default=None, max_length=20_000)


class ForkRequest(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    notes: str | None = Field(default=None, max_length=20_000)
    prompt_version_id: str | None = None
    agent_profile_id: str | None = None
    agent_profile_version_id: str | None = None
    #: Merged over the original inputs (keys set here replace the original values).
    inputs: dict[str, Any] = Field(default_factory=dict)


class DriftItem(BaseModel):
    field: str
    label: str
    original: Any = None
    current: Any = None
    message: str


class LineageRun(BaseModel):
    id: str
    title: str
    status: str
    source: str
    relation: str | None
    created_at: datetime


class VersionChoice(BaseModel):
    id: str
    version: int
    label: str
    created_at: datetime | None = None


class ProfileChoice(BaseModel):
    id: str
    name: str
    adapter_type: str
    versions: list[VersionChoice]


class ForkOptions(BaseModel):
    replayable: bool
    reason: str | None = None
    #: Fork (changed prompt, profile, or variables) applies to agent runs only.
    forkable: bool = True
    fork_reason: str | None = None
    prompt_template_id: str | None
    prompt_name: str | None
    prompt_version_id: str | None
    prompt_versions: list[VersionChoice]
    variables: list[dict[str, Any]]
    inputs: dict[str, Any]
    agent_profile_id: str | None
    agent_profile_version_id: str | None
    profiles: list[ProfileChoice]


class LineageOut(BaseModel):
    run_id: str
    parent: LineageRun | None
    children: list[LineageRun]
    drift: list[DriftItem]
    options: ForkOptions
    replay_of: dict[str, Any] | None = None
    fork_of: dict[str, Any] | None = None


class LineageCreateResult(BaseModel):
    result: RunCreateResult
    drift: list[DriftItem]
    changes: list[dict[str, Any]] = Field(default_factory=list)


# --------------------------------------------------------------------------- comparison


class RunSide(BaseModel):
    run: RunOut
    attempts: int
    event_count: int
    event_types: dict[str, int]
    usage: dict[str, Any] | None
    usage_status: str
    cost: dict[str, Any] | None
    evaluations: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]
    git: dict[str, Any]
    prompt: dict[str, Any] | None
    profile: dict[str, Any] | None
    inputs: dict[str, Any]


class Comparison(BaseModel):
    a: RunSide
    b: RunSide
    relation: str | None
    differences: dict[str, Any]


# --------------------------------------------------------------------------- retention


class RetentionSettings(BaseModel):
    max_age_days: int = Field(default=30, ge=1, le=3650)
    statuses: list[str] = Field(
        default_factory=lambda: ["succeeded", "failed", "timed_out", "cancelled", "skipped"]
    )
    include_demo: bool = True
    keep_tag: str = Field(default="keep", max_length=64)
    remove_worktrees: bool = True
    include_orphans: bool = True


class RetentionRunItem(BaseModel):
    run_id: str
    title: str
    status: str
    finished_at: datetime | None
    is_demo: bool
    artifact_bytes: int
    log_bytes: int
    worktree_bytes: int
    total_bytes: int
    worktree_path: str | None = None
    blocked: str | None = None


class RetentionOrphan(BaseModel):
    kind: str
    path: str
    bytes: int
    blocked: str | None = None


class RetentionPreview(BaseModel):
    settings: RetentionSettings
    cutoff: datetime
    generated_at: datetime
    runs: list[RetentionRunItem]
    orphans: list[RetentionOrphan]
    total_runs: int
    total_bytes: int
    blocked_runs: int


class RetentionExecute(BaseModel):
    run_ids: list[str] = Field(default_factory=list, max_length=5000)
    orphan_paths: list[str] = Field(default_factory=list, max_length=5000)


class RetentionResult(BaseModel):
    deleted_runs: list[str]
    skipped: list[dict[str, Any]]
    removed_paths: list[str]
    freed_bytes: int
