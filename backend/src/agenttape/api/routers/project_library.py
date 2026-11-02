"""Project path checks, activity, and first-run onboarding state."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from agenttape.api.deps import Actor, Ctx
from agenttape.application.schemas.library import (
    OnboardingState,
    OnboardingUpdate,
    PathCheckRequest,
    PathCheckResult,
    ProjectActivity,
)
from agenttape.application.services.onboarding import OnboardingService
from agenttape.application.services.project_library import ProjectLibraryService

router = APIRouter(tags=["projects"])


@router.post(
    "/projects/check-path",
    response_model=PathCheckResult,
    summary="Canonicalize a path and explain whether it can become a project",
)
def check_path(body: PathCheckRequest, ctx: Ctx) -> PathCheckResult:
    return ProjectLibraryService(ctx).check_path(body.path)


@router.get("/projects/{project_id}/activity", response_model=ProjectActivity)
def project_activity(
    project_id: str, ctx: Ctx, limit: Annotated[int, Query(ge=1, le=50)] = 10
) -> ProjectActivity:
    return ProjectLibraryService(ctx).activity(project_id, limit=limit)


@router.get("/onboarding", response_model=OnboardingState)
def onboarding_state(ctx: Ctx) -> OnboardingState:
    return OnboardingService(ctx).state()


@router.post("/onboarding", response_model=OnboardingState)
def update_onboarding(body: OnboardingUpdate, ctx: Ctx, actor: Actor) -> OnboardingState:
    return OnboardingService(ctx).update(body.action, actor=actor)
