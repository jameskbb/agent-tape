"""Request/response shapes for the capture ingest path (MCP server, Claude Code hooks).

Limits are imported from :mod:`agenttape.findings.citations` so the wire-level caps match
the caps ``FindingsService`` itself enforces exactly.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agenttape.findings.citations import (
    MAX_CITATIONS,
    MAX_EVENT_REF_CHARS,
    MAX_LINE,
    MAX_PATH_CHARS,
    MAX_QUOTE_CHARS,
)

MAX_KIND_CHARS = 32
MAX_TEXT_CHARS = 4000


class FileCitationIn(BaseModel):
    """A citation to a file line range: the quote is the evidence AgentTape checks; the
    line numbers are a hint AgentTape recomputes from the quote."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=MAX_PATH_CHARS)
    line_start: int | None = Field(default=None, ge=1, le=MAX_LINE)
    line_end: int | None = Field(default=None, ge=1, le=MAX_LINE)
    quote: str = Field(min_length=1, max_length=MAX_QUOTE_CHARS)


class EventCitationIn(BaseModel):
    """A citation to a previously recorded event: the ``tool_use_id`` of a tool call whose
    output you quote."""

    model_config = ConfigDict(extra="forbid")

    event_ref: str = Field(min_length=1, max_length=MAX_EVENT_REF_CHARS)
    quote: str = Field(min_length=1, max_length=MAX_QUOTE_CHARS)


class ReviewTargetIn(BaseModel):
    """Set only by an adversarial reviewer challenging another run's finding (the
    review contract). Capture validates the shape and passes it through unchanged; it never
    interprets it."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, max_length=36)
    finding_key: str = Field(min_length=1, max_length=200)
    outcome: Literal["upheld", "refuted", "inconclusive"]


class FindingRecordIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1, max_length=MAX_KIND_CHARS)
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    citations: list[FileCitationIn | EventCitationIn] = Field(
        default_factory=list, max_length=MAX_CITATIONS
    )
    target: ReviewTargetIn | None = None
    session_id: str | None = Field(default=None, max_length=200)
    cwd: str | None = Field(default=None, max_length=4096)
    instance_id: str | None = Field(default=None, max_length=64)


class FindingRecordOut(BaseModel):
    run_id: str
    event_id: str
    citations: int
