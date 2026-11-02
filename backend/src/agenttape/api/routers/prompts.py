"""Prompt templates and versions (diff, import/export, and archive live in prompt_library.py)."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query

from agenttape.api.deps import Actor, Ctx, Paging
from agenttape.application.schemas.catalog import (
    PromptTemplateCreate,
    PromptTemplateOut,
    PromptVersionCreate,
    PromptVersionOut,
    RenderPreview,
    RenderRequest,
)
from agenttape.application.schemas.common import Page
from agenttape.application.services.prompt_library import PromptLibraryService
from agenttape.application.services.prompts import PromptService

router = APIRouter(tags=["prompts"])


@router.get("/prompts", response_model=Page[PromptTemplateOut])
def list_prompts(
    ctx: Ctx,
    paging: Paging,
    q: Annotated[str | None, Query(max_length=200)] = None,
    include_archived: bool = False,
    tag: Annotated[list[str] | None, Query(max_length=20)] = None,
    owner: Annotated[str | None, Query(max_length=200)] = None,
    archived: Literal["active", "archived", "all"] | None = None,
    project_id: str | None = None,
    sort: Literal["name", "updated_at", "created_at"] = "name",
    order: Literal["asc", "desc"] = "asc",
) -> Page[PromptTemplateOut]:
    return PromptLibraryService(ctx).search(
        q=q,
        tags=tag,
        owner=owner,
        archived=archived or ("all" if include_archived else "active"),
        project_id=project_id,
        sort=sort,
        order=order,
        limit=paging.limit,
        offset=paging.offset,
    )


@router.post("/prompts", response_model=PromptTemplateOut, status_code=201)
def create_prompt(body: PromptTemplateCreate, ctx: Ctx, actor: Actor) -> PromptTemplateOut:
    return PromptService(ctx).create_template(body, actor=actor)


@router.get("/prompts/{template_id}", response_model=PromptTemplateOut)
def get_prompt(template_id: str, ctx: Ctx) -> PromptTemplateOut:
    return PromptService(ctx).get(template_id)


@router.get("/prompts/{template_id}/versions", response_model=list[PromptVersionOut])
def list_versions(template_id: str, ctx: Ctx) -> list[PromptVersionOut]:
    return PromptService(ctx).list_versions(template_id)


@router.post("/prompts/{template_id}/versions", response_model=PromptVersionOut, status_code=201)
def create_version(
    template_id: str, body: PromptVersionCreate, ctx: Ctx, actor: Actor
) -> PromptVersionOut:
    return PromptService(ctx).create_version(template_id, body, actor=actor)


@router.post(
    "/prompts/{template_id}/render",
    response_model=RenderPreview,
    summary="Preview a rendered prompt (secret references appear as [secret:NAME])",
)
def render_prompt(template_id: str, body: RenderRequest, ctx: Ctx) -> RenderPreview:
    return PromptService(ctx).preview(template_id, body.inputs, body.version_id)
