"""DTOs for the Findings UI read paths, every value taken from AgentTape's own record.

``CitationContentOut`` is the response of ``GET /citations/{citation_id}/content``:
numbered lines from the retained read a citation was checked
against, never the live file and never the agent's quote.

``ConfirmationIn``/``ConfirmationOut``/``FindingConfirmationOut`` back
``POST /findings/{finding_id}/confirm`` and ``.../confirm/withdraw``: the one human rung on the evidence ladder, derived from the append-only audit
log rather than a stored column.

``FindingRunOut``/``FindingRowOut``/``FindingsTallyOut`` back ``GET /findings`` (the
sidebar inbox) and the ``findings`` block of ``GET /dashboard``: one cross-run read model, so the inbox, the tile, and a run's own findings tab
agree on what a finding's worst verdict and "every quote matches" mean.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class CitationContentOut(BaseModel):
    path: str
    lines: list[tuple[int, str]]
    #: The retained read's own first/last line numbers (not the window returned).
    line_first: int | None
    line_last: int | None
    #: The lines actually returned (after context and the size cap).
    window: tuple[int, int] | None
    #: The rows to mark: the latest verdict's matched range, or the citation's claimed range.
    cited: tuple[int, int] | None
    truncated: bool
    redacted: bool
    #: True when the ``MAX_CONTENT_LINES`` cap withheld a line the citation points at: part
    #: of a longer ``cited`` range, or, for a citation with no range (``cited`` is None),
    #: the rest of a retained read longer than the cap (only its first lines are served).
    clipped: bool
    blob_ref: str | None
    #: The retained read's own tool-result event, always an event of the citation's run.
    read_result_event_id: str | None
    resolved_by: Literal["verdict_read", "covering_read"] | None
    unavailable_reason: str | None


class ConfirmationIn(BaseModel):
    """Body of both ``POST /findings/{finding_id}/confirm`` and its ``.../withdraw``."""

    note: str | None = Field(default=None, max_length=2000)


class ConfirmationOut(BaseModel):
    """A finding's confirmation, derived from the latest matching audit row (no column)."""

    actor: str
    ts: str
    note: str | None


class FindingConfirmationOut(BaseModel):
    finding_id: str
    confirmation: ConfirmationOut | None


class FindingRunOut(BaseModel):
    """The finding's run, named for display in the cross-run inbox: everything a row needs
    to link back to its run without a second lookup."""

    id: str
    title: str
    status: str
    kind: str
    #: ``agent_profile_name``, else ``adapter_type``, else the run kind, humanized.
    agent_label: str
    project_id: str | None
    project_name: str | None


class FindingRowOut(BaseModel):
    """One row of ``GET /findings``: a finding, its citation tally, and its run, in the one
    cross-run read model shared with the dashboard's ``findings`` block."""

    id: str
    key: str
    kind: str | None
    text: str | None
    created_at: datetime
    citations: int
    #: Latest verdict of each citation, keyed by verdict (a null latest verdict counts as
    #: "unchecked"); only non-zero keys are present.
    verdict_counts: dict[str, int]
    #: The worst verdict among the finding's citations (``declared_only`` with none).
    worst_verdict: str
    run: FindingRunOut


class FindingsTallyOut(BaseModel):
    """The ``findings`` block of ``GET /dashboard``: the same window and definitions as
    ``GET /findings``, with no filters, tallied instead of listed."""

    recorded: int
    #: Findings with at least one citation whose latest verdict is ``quote_matches`` for
    #: every citation (the same definition as the frontend ``model.ts``).
    all_quotes_match: int
    declared_only: int
    citations: int
    #: Citation counts by latest verdict; only non-zero keys are present.
    verdicts: dict[str, int]
    unchecked: int
