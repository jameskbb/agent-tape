"""Data retention settings, the cleanup dry-run preview, and cleanup."""

from __future__ import annotations

from fastapi import APIRouter

from agenttape.api.deps import Actor, Ctx
from agenttape.application.schemas.git import (
    RetentionExecute,
    RetentionPreview,
    RetentionResult,
    RetentionSettings,
)
from agenttape.application.services.retention import RetentionService

router = APIRouter(tags=["retention"])


@router.get("/retention/settings", response_model=RetentionSettings)
def get_settings(ctx: Ctx) -> RetentionSettings:
    return RetentionService(ctx).get_settings()


@router.put("/retention/settings", response_model=RetentionSettings)
def put_settings(body: RetentionSettings, ctx: Ctx, actor: Actor) -> RetentionSettings:
    return RetentionService(ctx).update_settings(body, actor=actor)


@router.post(
    "/retention/preview",
    response_model=RetentionPreview,
    summary="Dry run: what cleanup would delete and how many bytes (nothing is deleted)",
)
def preview(ctx: Ctx, body: RetentionSettings | None = None) -> RetentionPreview:
    return RetentionService(ctx).preview(body)


@router.post(
    "/retention/execute",
    response_model=RetentionResult,
    summary="Delete previewed runs and orphans that are still eligible (AgentTape data only)",
)
def execute(body: RetentionExecute, ctx: Ctx, actor: Actor) -> RetentionResult:
    return RetentionService(ctx).execute(body, actor=actor)
