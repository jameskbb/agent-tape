"""Evaluation definition, suite, ad hoc evaluation, human score, and comparison DTOs."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from agenttape.application.schemas.common import OrmModel
from agenttape.application.schemas.runs import EvaluationResultOut, EvaluatorConfig


class EvaluationDefinitionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    evaluator_type: str = Field(min_length=1, max_length=64)
    config: dict[str, Any] = Field(default_factory=dict)
    required: bool = False


class EvaluationDefinitionVersionCreate(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)
    required: bool = False


class EvaluationDefinitionUpdate(BaseModel):
    description: str | None = Field(default=None, max_length=4000)
    archived: bool | None = None


class EvaluationDefinitionVersionOut(OrmModel):
    id: str
    definition_id: str
    version: int
    config: dict[str, Any]
    required: bool
    content_hash: str
    created_at: datetime
    locked_at: datetime | None


class EvaluationDefinitionOut(OrmModel):
    id: str
    workspace_id: str
    name: str
    description: str | None
    evaluator_type: str
    archived: bool
    latest_version: int
    is_demo: bool
    created_at: datetime
    updated_at: datetime
    latest: EvaluationDefinitionVersionOut | None = None
    deterministic: bool | None = None


class SuiteMember(BaseModel):
    definition_id: str
    #: Pin a version number; ``None`` uses the latest version when the suite runs.
    version: int | None = None
    #: Override the definition's ``required`` flag inside this suite.
    required: bool | None = None


class EvaluationSuiteCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    members: list[SuiteMember] = Field(default_factory=list)


class EvaluationSuiteUpdate(BaseModel):
    description: str | None = Field(default=None, max_length=4000)
    members: list[SuiteMember] | None = None
    archived: bool | None = None


class EvaluationSuiteOut(OrmModel):
    id: str
    workspace_id: str
    name: str
    description: str | None
    members: list[dict[str, Any]]
    archived: bool
    is_demo: bool
    created_at: datetime
    updated_at: datetime


class EvaluateRunRequest(BaseModel):
    definition_ids: list[str] = Field(default_factory=list)
    suite_id: str | None = None
    evaluators: list[EvaluatorConfig] = Field(default_factory=list)


class RerunRequest(BaseModel):
    #: Specific results to rerun; empty reruns every deterministic result of the run.
    result_ids: list[str] = Field(default_factory=list)


class HumanScoreCreate(BaseModel):
    name: str = Field(default="Human review", min_length=1, max_length=200)
    score: float = Field(ge=0)
    max_score: float = Field(default=5, gt=0)
    pass_threshold: float | None = Field(default=None, ge=0)
    verdict: Literal["pass", "fail", "warning"] | None = None
    notes: str | None = Field(default=None, max_length=10_000)
    reviewer: str = Field(default="local user", min_length=1, max_length=200)
    required: bool = False


class EvaluationRunResponse(BaseModel):
    results: list[EvaluationResultOut]
    skipped: list[dict[str, Any]] = Field(default_factory=list)


class ComparisonCell(BaseModel):
    result_id: str
    status: str
    score: float | None
    explanation: str | None
    required: bool
    evaluator_version: str
    duration_ms: int | None


class ComparisonRow(BaseModel):
    name: str
    evaluator_type: str
    #: run id -> latest result for this evaluation (missing when that run lacks it)
    cells: dict[str, ComparisonCell]
    differs: bool


class ComparisonRunSummary(BaseModel):
    run_id: str
    title: str | None
    status: str | None
    counts: dict[str, int]
    required_failed: int


class EvaluationComparison(BaseModel):
    run_ids: list[str]
    runs: list[ComparisonRunSummary]
    rows: list[ComparisonRow]


class EvaluatorTypeInfo(BaseModel):
    type: str
    version: str
    display_name: str
    description: str
    deterministic: bool
    config_schema: dict[str, Any]
    available: bool = True
    unavailable_reason: str | None = None
