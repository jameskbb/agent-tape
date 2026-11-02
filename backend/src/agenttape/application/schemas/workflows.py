"""Workflow DTOs (definitions, versions, validation, starting a workflow run)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from agenttape.application.schemas.common import OrmModel


class WorkflowCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    tags: list[str] = Field(default_factory=list)
    project_id: str | None = None
    #: Initial definition (nodes, edges, inputs, settings); saved as version 1.
    definition: dict[str, Any]
    notes: str | None = Field(default=None, max_length=4000)


class WorkflowUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    tags: list[str] | None = None
    archived: bool | None = None


class WorkflowVersionCreate(BaseModel):
    definition: dict[str, Any]
    notes: str | None = Field(default=None, max_length=4000)


class ValidationIssue(BaseModel):
    path: str
    message: str
    severity: str = "error"


class WorkflowValidation(BaseModel):
    valid: bool
    issues: list[ValidationIssue]
    #: Topological order of node keys when the graph is acyclic.
    order: list[str] | None = None


class WorkflowVersionOut(OrmModel):
    id: str
    workflow_id: str
    version: int
    definition: dict[str, Any]
    input_schema: dict[str, Any] | None
    content_hash: str
    notes: str | None
    created_by: str | None
    created_at: datetime
    locked_at: datetime | None


class WorkflowOut(OrmModel):
    id: str
    workspace_id: str
    project_id: str | None
    name: str
    description: str | None
    tags: list[str]
    archived: bool
    latest_version: int
    max_concurrency: int | None
    is_demo: bool
    created_at: datetime
    updated_at: datetime
    latest: WorkflowVersionOut | None = None
    node_count: int | None = None
    last_run_status: str | None = None
    last_run_id: str | None = None
    last_run_at: datetime | None = None


class WorkflowRunCreate(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)
    version_id: str | None = None
    title: str | None = Field(default=None, max_length=300)
    priority: int = Field(default=0, ge=-100, le=100)
    idempotency_key: str | None = Field(default=None, max_length=255)
    tags: list[str] = Field(default_factory=list)
    notes: str | None = None


class NodeTypeInfo(BaseModel):
    type: str
    label: str
    description: str
    config_fields: list[dict[str, Any]]
