"""DTOs for the citation verification engine."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class VerificationSummary(BaseModel):
    run_id: str
    citations: int
    recorded: int
    unchanged: int = 0
    verdicts: dict[str, int] = Field(default_factory=dict)


class CitationReproductionOut(BaseModel):
    """One ``citation_reproductions`` row: a separate execution result, never a verdict.

    The same fields as the per-citation ``reproduction`` the findings payload embeds, so
    the ``POST`` response and the embedded summary share one client type.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    trigger: str
    mode: str
    status: str
    result: str | None
    reason: str | None
    pinned_commit: str | None
    exit_code: int | None
    recorded_exit_code: int | None
    created_at: datetime
    finished_at: datetime | None
