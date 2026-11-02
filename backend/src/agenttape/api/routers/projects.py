"""Projects and the allowlisted project roots."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from agenttape.api.deps import Actor, Ctx, Paging
from agenttape.application.schemas.catalog import ProjectCreate, ProjectOut, ProjectUpdate
from agenttape.application.schemas.common import Page
from agenttape.application.services.audit import record_audit
from agenttape.application.services.projects import ProjectService
from agenttape.application.services.workspaces import (
    ALLOWED_ROOTS_KEY,
    allowed_roots,
    get_app_setting,
    set_app_setting,
)
from agenttape.domain.errors import ValidationError
from agenttape.security.paths import canonicalize

router = APIRouter(tags=["projects"])


@router.get("/projects", response_model=Page[ProjectOut])
def list_projects(
    ctx: Ctx,
    paging: Paging,
    q: Annotated[str | None, Query(max_length=200)] = None,
    include_archived: bool = False,
) -> Page[ProjectOut]:
    return ProjectService(ctx).list_projects(
        search=q, include_archived=include_archived, limit=paging.limit, offset=paging.offset
    )


@router.post("/projects", response_model=ProjectOut, status_code=201)
def create_project(body: ProjectCreate, ctx: Ctx, actor: Actor) -> ProjectOut:
    return ProjectService(ctx).create(body, actor=actor)


@router.get("/projects/{project_id}", response_model=ProjectOut)
def get_project(project_id: str, ctx: Ctx) -> ProjectOut:
    return ProjectService(ctx).get(project_id)


@router.patch("/projects/{project_id}", response_model=ProjectOut)
def update_project(project_id: str, body: ProjectUpdate, ctx: Ctx, actor: Actor) -> ProjectOut:
    return ProjectService(ctx).update(project_id, body, actor=actor)


@router.post("/projects/{project_id}/refresh-git", response_model=ProjectOut)
def refresh_git(project_id: str, ctx: Ctx) -> ProjectOut:
    return ProjectService(ctx).refresh_git_state(project_id)


class AllowedRoots(BaseModel):
    #: Roots saved in the database (editable here).
    roots: list[str] = Field(default_factory=list)
    #: Effective allowlist: environment + saved + AgentTape's own fixtures directory.
    effective: list[str] = Field(default_factory=list)


@router.get("/settings/allowed-roots", response_model=AllowedRoots)
def get_allowed_roots(ctx: Ctx) -> AllowedRoots:
    with ctx.session() as session:
        stored = (get_app_setting(session, ALLOWED_ROOTS_KEY) or {}).get("roots", [])
        effective = [str(p) for p in allowed_roots(session, ctx.settings)]
    return AllowedRoots(roots=list(stored), effective=effective)


class AllowedRootsUpdate(BaseModel):
    roots: list[str] = Field(default_factory=list, max_length=50)


@router.put("/settings/allowed-roots", response_model=AllowedRoots)
def put_allowed_roots(body: AllowedRootsUpdate, ctx: Ctx, actor: Actor) -> AllowedRoots:
    cleaned: list[str] = []
    for raw in body.roots:
        path = canonicalize(raw, must_exist=True)
        if path == path.parent:
            raise ValidationError("The filesystem root cannot be an allowlisted project root")
        if not path.is_dir():
            raise ValidationError("Allowed roots must be directories", details={"path": str(path)})
        if str(path) not in cleaned:
            cleaned.append(str(path))
    with ctx.session() as session:
        set_app_setting(session, ctx.clock, ALLOWED_ROOTS_KEY, {"roots": cleaned})
        record_audit(
            session,
            ctx.clock,
            action="settings.allowed_roots.update",
            resource_type="settings",
            resource_id=None,
            actor=actor,
            details={"roots": cleaned},
        )
        session.flush()
        effective = [str(p) for p in allowed_roots(session, ctx.settings)]
    return AllowedRoots(roots=cleaned, effective=effective)
