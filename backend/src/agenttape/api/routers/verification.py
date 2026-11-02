"""Re-checking a finished run's citations against AgentTape's own recording."""

from __future__ import annotations

from fastapi import APIRouter

from agenttape.api.deps import Ctx
from agenttape.application.schemas.verification import VerificationSummary
from agenttape.application.services.verification import VerificationService

router = APIRouter(tags=["findings"])


@router.post(
    "/runs/{run_id}/findings/verify",
    response_model=VerificationSummary,
    summary="Re-check a finished run's citations against AgentTape's recording (appends verdicts)",
)
def verify_run(run_id: str, ctx: Ctx) -> VerificationSummary:
    return VerificationService(ctx).verify_run(run_id, trigger="recheck")
