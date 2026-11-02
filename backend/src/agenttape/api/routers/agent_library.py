"""Agent profile admin, secret references (metadata only), and the executable allowlist.
Profile create/version/validate and adapter discovery live in ``agents.py``."""

from __future__ import annotations

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from agenttape.api.deps import Actor, Ctx
from agenttape.application.schemas.catalog import AgentProfileOut
from agenttape.application.schemas.library import (
    AgentProfileUpdate,
    ExecutableAllowlist,
    ExecutableAllowlistUpdate,
    ExecutableCheckOut,
    SecretReferenceCreate,
    SecretReferenceOut,
    SecretReferenceUpdate,
)
from agenttape.application.services.profile_library import ProfileLibraryService

router = APIRouter(tags=["agents"])


@router.patch("/agent-profiles/{profile_id}", response_model=AgentProfileOut)
def update_profile(
    profile_id: str, body: AgentProfileUpdate, ctx: Ctx, actor: Actor
) -> AgentProfileOut:
    """Name, description, tags, project, archived. Config changes create a new version."""
    return ProfileLibraryService(ctx).update(profile_id, body, actor=actor)


@router.get(
    "/secret-references",
    response_model=list[SecretReferenceOut],
    summary="Secret reference metadata (values are never returned)",
)
def list_secrets(ctx: Ctx) -> list[SecretReferenceOut]:
    return ProfileLibraryService(ctx).list_secrets()


@router.post("/secret-references", response_model=SecretReferenceOut, status_code=201)
def create_secret(body: SecretReferenceCreate, ctx: Ctx, actor: Actor) -> SecretReferenceOut:
    return ProfileLibraryService(ctx).create_secret(body, actor=actor)


@router.patch("/secret-references/{secret_id}", response_model=SecretReferenceOut)
def update_secret(
    secret_id: str, body: SecretReferenceUpdate, ctx: Ctx, actor: Actor
) -> SecretReferenceOut:
    return ProfileLibraryService(ctx).update_secret(secret_id, body, actor=actor)


@router.delete("/secret-references/{secret_id}", status_code=204)
def delete_secret(secret_id: str, ctx: Ctx, actor: Actor) -> Response:
    ProfileLibraryService(ctx).delete_secret(secret_id, actor=actor)
    return Response(status_code=204)


@router.get("/settings/allowed-executables", response_model=ExecutableAllowlist)
def get_executables(ctx: Ctx) -> ExecutableAllowlist:
    return ProfileLibraryService(ctx).executables()


@router.put("/settings/allowed-executables", response_model=ExecutableAllowlist)
def put_executables(body: ExecutableAllowlistUpdate, ctx: Ctx, actor: Actor) -> ExecutableAllowlist:
    return ProfileLibraryService(ctx).set_executables(body.executables, actor=actor)


class ExecutableCheckRequest(BaseModel):
    executable: str = Field(min_length=1, max_length=4096)
    adapter_type: str | None = None


@router.post(
    "/settings/allowed-executables/check",
    response_model=ExecutableCheckOut,
    summary="Resolve an executable and report whether it may run",
)
def check_executable(body: ExecutableCheckRequest, ctx: Ctx) -> ExecutableCheckOut:
    return ProfileLibraryService(ctx).check_executable(body.executable, body.adapter_type)
