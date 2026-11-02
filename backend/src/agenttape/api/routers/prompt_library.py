"""Prompt library: metadata edits, archive, duplicate, diff, import/export, amend unused
versions, and "Test with Demo Agent". Create/list/version/render live in
``prompts.py``."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from agenttape.api.deps import Actor, Ctx
from agenttape.application.schemas.catalog import (
    PromptTemplateOut,
    PromptVersionCreate,
    PromptVersionOut,
)
from agenttape.application.schemas.library import (
    PromptDiff,
    PromptDuplicateRequest,
    PromptExportBundle,
    PromptImportResult,
    PromptTemplateUpdate,
    PromptTestRequest,
    PromptTestResult,
    PromptVersionAmend,
)
from agenttape.application.services.prompt_library import PromptLibraryService

router = APIRouter(tags=["prompts"])


@router.get("/prompt-tags", response_model=list[str], summary="Every tag used by a prompt")
def prompt_tags(ctx: Ctx) -> list[str]:
    return PromptLibraryService(ctx).all_tags()


class ExportRequest(BaseModel):
    #: Empty exports every prompt (including archived ones).
    template_ids: list[str] = Field(default_factory=list, max_length=500)


@router.post("/prompts/export", response_model=PromptExportBundle, summary="Export prompts as JSON")
def export_prompts(body: ExportRequest, ctx: Ctx) -> PromptExportBundle:
    return PromptLibraryService(ctx).export(body.template_ids or None)


@router.post(
    "/prompts/import",
    response_model=PromptImportResult,
    status_code=201,
    summary="Import an export bundle; existing prompts are never overwritten",
)
def import_prompts(
    body: PromptExportBundle,
    ctx: Ctx,
    actor: Actor,
    on_conflict: Literal["skip", "rename"] = "rename",
) -> PromptImportResult:
    return PromptLibraryService(ctx).import_bundle(body, on_conflict=on_conflict, actor=actor)


@router.patch("/prompts/{template_id}", response_model=PromptTemplateOut)
def update_prompt(
    template_id: str, body: PromptTemplateUpdate, ctx: Ctx, actor: Actor
) -> PromptTemplateOut:
    return PromptLibraryService(ctx).update(template_id, body, actor=actor)


@router.post("/prompts/{template_id}/duplicate", response_model=PromptTemplateOut, status_code=201)
def duplicate_prompt(
    template_id: str, body: PromptDuplicateRequest, ctx: Ctx, actor: Actor
) -> PromptTemplateOut:
    return PromptLibraryService(ctx).duplicate(
        template_id, name=body.name, version_id=body.version_id, actor=actor
    )


@router.post(
    "/prompts/{template_id}/versions/save",
    response_model=PromptVersionOut,
    status_code=201,
    summary="Save an edit as a new version (409 when nothing changed)",
)
def save_version(
    template_id: str, body: PromptVersionCreate, ctx: Ctx, actor: Actor
) -> PromptVersionOut:
    return PromptLibraryService(ctx).save_version(template_id, body, actor=actor)


@router.get("/prompts/{template_id}/versions/{version_id}", response_model=PromptVersionOut)
def get_version(template_id: str, version_id: str, ctx: Ctx) -> PromptVersionOut:
    return PromptLibraryService(ctx).get_version(template_id, version_id)


@router.put(
    "/prompts/{template_id}/versions/{version_id}",
    response_model=PromptVersionOut,
    summary="Amend a version no run has used (409 version_immutable otherwise)",
)
def amend_version(
    template_id: str, version_id: str, body: PromptVersionAmend, ctx: Ctx, actor: Actor
) -> PromptVersionOut:
    return PromptLibraryService(ctx).amend_version(template_id, version_id, body, actor=actor)


@router.get("/prompts/{template_id}/diff", response_model=PromptDiff)
def diff_versions(
    template_id: str,
    ctx: Ctx,
    from_version: Annotated[int, Query(alias="from", ge=1)],
    to_version: Annotated[int, Query(alias="to", ge=1)],
) -> PromptDiff:
    return PromptLibraryService(ctx).diff(template_id, from_version, to_version)


@router.post(
    "/prompts/{template_id}/test-run",
    response_model=PromptTestResult,
    status_code=201,
    summary="Run this prompt with the Demo Agent through the normal run engine",
)
def test_run(template_id: str, body: PromptTestRequest, ctx: Ctx, actor: Actor) -> PromptTestResult:
    return PromptLibraryService(ctx).test_with_demo(
        template_id, body.inputs, version_id=body.version_id, actor=actor
    )
