"""API schemas for File Evidence (documented in OpenAPI through the router)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from agenttape.application.schemas.common import OrmModel

Classification = Literal[
    "observed_read",
    "inferred_access",
    "created",
    "modified",
    "renamed",
    "deleted",
    "restored",
    "net_unchanged",
    "generated",
    "outside_boundary",
    "unknown",
]
SortKey = Literal["first_activity", "last_activity", "operations", "path", "diff_size"]


class RunFileOut(OrmModel):
    """One logical file (or one collapsed noisy directory) in a run's file ledger."""

    id: str
    run_id: str
    file_key: str
    start_path: str | None
    end_path: str | None
    rename_chain: list[str]
    first_at: datetime | None
    last_at: datetime | None
    operation_counts: dict[str, int]
    operation_total: int
    first_step: str | None
    last_step: str | None
    attempts: list[str]
    agents: list[str]
    tools: list[str]
    sources: list[str]
    classification: str
    label: str = ""
    classifications: list[str]
    start_hash: str | None
    end_hash: str | None
    size_bytes: int | None
    is_binary: bool | None
    net_status: str
    insertions: int | None
    deletions: int | None
    net_unchanged: bool
    is_protected: bool
    protected_patterns: list[str]
    is_generated: bool
    is_outside: bool
    is_collapsed: bool
    collapsed_count: int | None
    completeness: str
    explanation: str
    policy: dict[str, Any] | None


class EvidenceEventOut(OrmModel):
    """One raw, append-only evidence record."""

    id: str
    run_id: str
    attempt_id: str | None
    node_key: str | None
    sequence: int
    ts: datetime
    last_ts: datetime | None
    operation: str
    path: str
    previous_path: str | None
    inside_boundary: bool
    source: str
    quality: str
    adapter: str | None
    runtime: str | None
    tool: str | None
    command: str | None
    correlation_id: str | None
    source_event_id: str | None
    before_hash: str | None
    after_hash: str | None
    size_bytes: int | None
    is_binary: bool | None
    count: int
    protected_patterns: list[str]
    policy: dict[str, Any] | None
    redacted: bool
    details: dict[str, Any]


class Completeness(BaseModel):
    """The honest evidence completeness statement for a run."""

    level: Literal["complete", "partial", "unavailable", "pending"]
    statement: str
    writes: str
    reads: str
    git: str
    step_attribution: str
    offline_windows: list[dict[str, Any]] = Field(default_factory=list)
    dropped_watcher_events: int = 0
    start: dict[str, Any] | None = None
    end: dict[str, Any] | None = None


class FileCounts(BaseModel):
    """Run summary counts. ``None`` means unavailable (never a misleading zero)."""

    files: int
    read_or_referenced: int | None
    created: int | None
    modified: int | None
    deleted: int | None
    renamed: int | None
    restored_or_net_unchanged: int | None
    generated: int | None
    protected_findings: int
    outside_findings: int
    collapsed_events: int
    collapsed_directories: int


class CollapsedDir(BaseModel):
    path: str
    count: int
    by_operation: dict[str, int]


class Finding(BaseModel):
    file_key: str
    classification: str
    net_status: str
    patterns: list[str] = Field(default_factory=list)
    policy: dict[str, Any] | None = None


class RunFileEvidenceSummary(BaseModel):
    run_id: str
    run_status: str
    completeness: Completeness
    counts: FileCounts
    unavailable: dict[str, str] = Field(
        default_factory=dict, description="Why a count is null (key: count name)"
    )
    collapsed: list[CollapsedDir]
    protected: list[Finding]
    outside: list[Finding]
    facets: dict[str, list[str]] = Field(
        default_factory=dict, description="Values present in this run, for filter controls"
    )


class LinkedEvent(BaseModel):
    id: str
    sequence: int
    type: str
    summary: str
    ts: datetime


class LinkedApproval(BaseModel):
    id: str
    title: str
    status: str
    type: str
    reviewer: str | None
    decided_at: datetime | None


class FileState(BaseModel):
    path: str | None
    hash: str | None
    size_bytes: int | None
    is_binary: bool | None
    exists: bool | None = Field(description="None when the state was not captured")


class FileDiff(BaseModel):
    path: str
    change_type: str
    insertions: int | None
    deletions: int | None
    is_binary: bool
    hunks: list[dict[str, Any]]
    diff_available: bool
    diff_truncated: bool
    #: Why the file has no hunk when AgentTape left it out on purpose (``ChangedFileOut``).
    diff_omitted: Literal["size_cap", "count_cap"] | None = None


class FileTimeline(BaseModel):
    file: RunFileOut
    events: list[EvidenceEventOut]
    truncated: bool = Field(description="True when more evidence exists than was returned")
    start_state: FileState
    end_state: FileState
    diff: FileDiff | None
    linked_events: list[LinkedEvent]
    approvals: list[LinkedApproval]


class RebuildResult(BaseModel):
    run_id: str
    reconciled: bool
    files: int
    summary: RunFileEvidenceSummary


class ActivityEntry(BaseModel):
    path: str
    runs: int
    operations: int
    last_at: datetime | None
    run_ids: list[str]


class ChangedTogether(BaseModel):
    paths: list[str]
    runs: int
    run_ids: list[str]


class RunFileRef(BaseModel):
    path: str
    run_id: str
    run_title: str
    run_status: str
    classification: str
    net_status: str
    last_at: datetime | None
    policy: dict[str, Any] | None = None


class ProjectFileActivity(BaseModel):
    project_id: str
    runs_scanned: int
    runs_with_evidence: int
    most_touched: list[ActivityEntry]
    failed_run_files: list[ActivityEntry]
    changed_together: list[ChangedTogether]
    protected_activity: list[RunFileRef]
    recent: list[RunFileRef]


class FileRunHistoryItem(BaseModel):
    run_id: str
    run_title: str
    run_status: str
    classification: str
    net_status: str
    net_unchanged: bool
    operation_total: int
    first_at: datetime | None
    last_at: datetime | None
    is_protected: bool
    insertions: int | None
    deletions: int | None
