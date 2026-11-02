"""Challenge a finished run's findings with a chosen agent profile (on-demand review), the
retained-content read behind the Findings UI's citation viewer,
confirming or withdrawing confirmation of a finding, and the
cross-run findings inbox."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from agenttape.api.deps import Actor, Ctx, Paging
from agenttape.application.schemas.common import Page
from agenttape.application.schemas.finding_review import (
    ChallengeCreate,
    ChallengeOut,
    ChallengeStartResult,
)
from agenttape.application.schemas.findings import (
    CitationContentOut,
    ConfirmationIn,
    FindingConfirmationOut,
    FindingRowOut,
)
from agenttape.application.services.dashboard import MAX_WINDOW_DAYS
from agenttape.application.services.finding_review import FindingReviewService
from agenttape.application.services.findings import (
    INBOX_VERDICTS,
    MAX_CONTENT_CONTEXT,
    FindingsService,
)

router = APIRouter(tags=["findings"])

_VERDICT_PATTERN = "^(" + "|".join(INBOX_VERDICTS) + ")$"


@router.get(
    "/findings",
    response_model=Page[FindingRowOut],
    summary="Findings across every run in a window, flagged verdicts first",
)
def list_findings(
    ctx: Ctx,
    paging: Paging,
    days: Annotated[int, Query(ge=1, le=MAX_WINDOW_DAYS)] = 14,
    project_id: str | None = None,
    agent_profile_id: str | None = None,
    verdict: Annotated[str | None, Query(pattern=_VERDICT_PATTERN)] = None,
    kind: str | None = None,
    source: Annotated[str | None, Query(pattern="^(launched|observed)$")] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    sort: Annotated[str, Query(pattern="^(flagged|newest)$")] = "flagged",
) -> Page[FindingRowOut]:
    return FindingsService(ctx).list_across_runs(
        days=days,
        project_id=project_id,
        agent_profile_id=agent_profile_id,
        verdict=verdict,
        kind=kind,
        source=source,
        q=q,
        sort=sort,
        limit=paging.limit,
        offset=paging.offset,
    )


@router.post(
    "/runs/{run_id}/challenge",
    response_model=ChallengeStartResult,
    status_code=201,
    summary="Challenge a finished run's findings with a chosen agent profile",
)
def challenge(run_id: str, body: ChallengeCreate, ctx: Ctx, actor: Actor) -> ChallengeStartResult:
    return FindingReviewService(ctx).start_challenge(run_id, body, actor=actor)


@router.get(
    "/runs/{run_id}/challenges",
    response_model=list[ChallengeOut],
    summary="Challenges of a run's findings, labelled model judgment",
)
def challenges(run_id: str, ctx: Ctx) -> list[ChallengeOut]:
    return FindingReviewService(ctx).challenges(run_id)


@router.get(
    "/citations/{citation_id}/content",
    response_model=CitationContentOut,
    summary="Cited lines from the retained read a citation was checked against",
)
def citation_content(
    citation_id: str,
    ctx: Ctx,
    context: Annotated[int, Query(ge=0, le=MAX_CONTENT_CONTEXT)] = 3,
    before: Annotated[int | None, Query(ge=0, le=MAX_CONTENT_CONTEXT)] = None,
    after: Annotated[int | None, Query(ge=0, le=MAX_CONTENT_CONTEXT)] = None,
) -> CitationContentOut:
    return FindingsService(ctx).citation_content(
        citation_id,
        before=before if before is not None else context,
        after=after if after is not None else context,
    )


@router.post(
    "/findings/{finding_id}/confirm",
    response_model=FindingConfirmationOut,
    summary="Confirm a finding: a person reviewed it and its evidence",
)
def confirm_finding(
    finding_id: str, body: ConfirmationIn, ctx: Ctx, actor: Actor
) -> FindingConfirmationOut:
    return FindingConfirmationOut.model_validate(
        FindingsService(ctx).confirm(finding_id, actor=actor, note=body.note)
    )


@router.post(
    "/findings/{finding_id}/confirm/withdraw",
    response_model=FindingConfirmationOut,
    summary="Withdraw a finding's confirmation",
)
def withdraw_finding_confirmation(
    finding_id: str, body: ConfirmationIn, ctx: Ctx, actor: Actor
) -> FindingConfirmationOut:
    return FindingConfirmationOut.model_validate(
        FindingsService(ctx).withdraw_confirmation(finding_id, actor=actor, note=body.note)
    )
