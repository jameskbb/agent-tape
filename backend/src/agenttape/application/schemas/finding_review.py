"""DTOs for challenging a finished run's findings (on-demand review)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from agenttape.application.schemas.runs import RunOut


class ChallengeCreate(BaseModel):
    agent_profile_id: str = Field(min_length=1)
    agent_profile_version_id: str | None = None
    title: str | None = Field(default=None, max_length=300)


class ChallengeStartResult(BaseModel):
    run: RunOut
    findings_reviewed: list[str]
    omitted: int
    neutralized: list[dict[str, str]]


class ReviewOutcomeOut(BaseModel):
    target_finding_key: str
    target_finding_id: str
    outcome: str
    review_finding_id: str
    review_finding_key: str
    text: str
    citations: int


class UnresolvedReviewOut(BaseModel):
    review_finding_id: str
    review_finding_key: str
    reason: str | None


class ChallengeOut(BaseModel):
    run: RunOut
    trigger: Literal["on_demand", "workflow_node"]
    state: Literal["pending", "completed", "did_not_complete"]
    findings_reviewed: list[str]
    omitted: int
    outcomes: list[ReviewOutcomeOut]
    unresolved: list[UnresolvedReviewOut]
    #: Every outcome above is model judgment, never proof; labelled once, here.
    judgment: Literal["model judgment"]
    counts_as_proof: Literal[False]
