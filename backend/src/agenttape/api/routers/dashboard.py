"""Overview dashboard aggregates (computed from recorded runs; unknown values are null)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from agenttape.api.deps import Ctx
from agenttape.application.services.dashboard import MAX_WINDOW_DAYS, Dashboard, DashboardService

router = APIRouter(tags=["dashboard"])


@router.get(
    "/dashboard", response_model=Dashboard, summary="Overview: live work, outcomes, activity"
)
def dashboard(ctx: Ctx, days: Annotated[int, Query(ge=1, le=MAX_WINDOW_DAYS)] = 14) -> Dashboard:
    return DashboardService(ctx).summary(days=days)
