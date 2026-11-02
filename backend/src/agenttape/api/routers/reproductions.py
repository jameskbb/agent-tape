"""Re-running the recorded argv behind an event citation.

The re-run itself always happens later, in a queued worker job, never in this request; the
result is a separate ``citation_reproductions`` row that never overwrites the citation's own
verdict.
"""

from __future__ import annotations

from fastapi import APIRouter

from agenttape.api.deps import Actor, Ctx
from agenttape.application.schemas.verification import CitationReproductionOut
from agenttape.application.services.reproductions import ReproductionService

router = APIRouter(tags=["findings"])


@router.post(
    "/citations/{citation_id}/reproductions",
    response_model=CitationReproductionOut,
    status_code=202,
    summary=(
        "Re-run the command a citation cites, as a queued job, through the command policy "
        "in an isolated snapshot worktree; the result never changes the citation's verdict"
    ),
)
def request_reproduction(citation_id: str, ctx: Ctx, actor: Actor) -> CitationReproductionOut:
    return ReproductionService(ctx).request(citation_id, trigger="user", actor=actor)
