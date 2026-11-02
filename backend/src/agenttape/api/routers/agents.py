"""Agent profiles, profile versions, validation, and adapter discovery."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from agenttape.api.deps import Actor, Ctx, Paging
from agenttape.application.schemas.catalog import (
    AdapterInfo,
    AgentProfileCreate,
    AgentProfileOut,
    AgentProfileVersionCreate,
    AgentProfileVersionOut,
    ValidationIssueOut,
)
from agenttape.application.schemas.common import Page
from agenttape.application.services.profiles import AgentProfileService

router = APIRouter(tags=["agents"])


@router.get(
    "/adapters", response_model=list[AdapterInfo], summary="Installed adapters and their status"
)
def list_adapters(ctx: Ctx) -> list[AdapterInfo]:
    return AgentProfileService(ctx).adapters()


@router.get("/agent-profiles", response_model=Page[AgentProfileOut])
def list_profiles(
    ctx: Ctx,
    paging: Paging,
    q: Annotated[str | None, Query(max_length=200)] = None,
    adapter_type: str | None = None,
    include_archived: bool = False,
) -> Page[AgentProfileOut]:
    return AgentProfileService(ctx).list_profiles(
        search=q,
        adapter_type=adapter_type,
        include_archived=include_archived,
        limit=paging.limit,
        offset=paging.offset,
    )


@router.post("/agent-profiles", response_model=AgentProfileOut, status_code=201)
def create_profile(body: AgentProfileCreate, ctx: Ctx, actor: Actor) -> AgentProfileOut:
    return AgentProfileService(ctx).create(body, actor=actor)


class ProfileValidateRequest(BaseModel):
    adapter_type: str
    config: dict[str, Any] = Field(default_factory=dict)


class ProfileValidateResult(BaseModel):
    valid: bool
    issues: list[ValidationIssueOut]


@router.post(
    "/agent-profiles/validate",
    response_model=ProfileValidateResult,
    summary="Validate a profile configuration without saving it",
)
def validate_profile(body: ProfileValidateRequest, ctx: Ctx) -> ProfileValidateResult:
    issues = AgentProfileService(ctx).validate_config(body.adapter_type, body.config)
    return ProfileValidateResult(
        valid=not any(i.severity == "error" for i in issues), issues=issues
    )


@router.get("/agent-profiles/{profile_id}", response_model=AgentProfileOut)
def get_profile(profile_id: str, ctx: Ctx) -> AgentProfileOut:
    return AgentProfileService(ctx).get(profile_id)


@router.get("/agent-profiles/{profile_id}/versions", response_model=list[AgentProfileVersionOut])
def list_profile_versions(profile_id: str, ctx: Ctx) -> list[AgentProfileVersionOut]:
    return AgentProfileService(ctx).list_versions(profile_id)


@router.post(
    "/agent-profiles/{profile_id}/versions", response_model=AgentProfileVersionOut, status_code=201
)
def create_profile_version(
    profile_id: str, body: AgentProfileVersionCreate, ctx: Ctx, actor: Actor
) -> AgentProfileVersionOut:
    return AgentProfileService(ctx).create_version(profile_id, body, actor=actor)
