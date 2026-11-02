"""Workflows: versioned DAG definitions, validation, node types, and starting runs."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Query, Response

from agenttape.api.deps import Actor, Ctx, Paging
from agenttape.application.schemas.common import Page
from agenttape.application.schemas.runs import RunCreateResult
from agenttape.application.schemas.workflows import (
    NodeTypeInfo,
    WorkflowCreate,
    WorkflowOut,
    WorkflowRunCreate,
    WorkflowUpdate,
    WorkflowValidation,
    WorkflowVersionCreate,
    WorkflowVersionOut,
)
from agenttape.application.services.workflows import WorkflowService, node_types
from agenttape.domain.enums import RunSource

router = APIRouter(tags=["workflows"])


@router.get("/workflow-node-types", response_model=list[NodeTypeInfo])
def list_node_types() -> list[NodeTypeInfo]:
    return node_types()


@router.post(
    "/workflows/validate",
    response_model=WorkflowValidation,
    summary="Validate a definition (acyclic, known node types, references) without saving",
)
def validate_workflow(body: WorkflowVersionCreate, ctx: Ctx) -> WorkflowValidation:
    return WorkflowService(ctx).validate(body.definition)


@router.get("/workflows", response_model=Page[WorkflowOut])
def list_workflows(
    ctx: Ctx,
    paging: Paging,
    q: Annotated[str | None, Query(max_length=200)] = None,
    include_archived: bool = False,
    project_id: str | None = None,
) -> Page[WorkflowOut]:
    return WorkflowService(ctx).list_workflows(
        search=q,
        include_archived=include_archived,
        project_id=project_id,
        limit=paging.limit,
        offset=paging.offset,
    )


@router.post("/workflows", response_model=WorkflowOut, status_code=201)
def create_workflow(body: WorkflowCreate, ctx: Ctx, actor: Actor) -> WorkflowOut:
    return WorkflowService(ctx).create(body, actor=actor)


@router.get("/workflows/{workflow_id}", response_model=WorkflowOut)
def get_workflow(workflow_id: str, ctx: Ctx) -> WorkflowOut:
    return WorkflowService(ctx).get(workflow_id)


@router.patch("/workflows/{workflow_id}", response_model=WorkflowOut)
def update_workflow(workflow_id: str, body: WorkflowUpdate, ctx: Ctx, actor: Actor) -> WorkflowOut:
    return WorkflowService(ctx).update(workflow_id, body, actor=actor)


@router.get("/workflows/{workflow_id}/versions", response_model=list[WorkflowVersionOut])
def list_versions(workflow_id: str, ctx: Ctx) -> list[WorkflowVersionOut]:
    return WorkflowService(ctx).list_versions(workflow_id)


@router.post(
    "/workflows/{workflow_id}/versions",
    response_model=WorkflowVersionOut,
    status_code=201,
    summary="Save a new immutable version",
)
def create_version(
    workflow_id: str, body: WorkflowVersionCreate, ctx: Ctx, actor: Actor
) -> WorkflowVersionOut:
    return WorkflowService(ctx).create_version(workflow_id, body, actor=actor)


@router.get("/workflows/{workflow_id}/versions/{version_id}", response_model=WorkflowVersionOut)
def get_version(workflow_id: str, version_id: str, ctx: Ctx) -> WorkflowVersionOut:
    return WorkflowService(ctx).get_version(workflow_id, version_id)


@router.post(
    "/workflows/{workflow_id}/runs",
    response_model=RunCreateResult,
    status_code=201,
    summary="Start a workflow run (idempotent with Idempotency-Key)",
    responses={200: {"model": RunCreateResult, "description": "Existing run for this key"}},
)
def start_workflow_run(
    workflow_id: str,
    body: WorkflowRunCreate,
    ctx: Ctx,
    actor: Actor,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=255)] = None,
) -> RunCreateResult:
    if idempotency_key and not body.idempotency_key:
        body = body.model_copy(update={"idempotency_key": idempotency_key})
    result = WorkflowService(ctx).start_run(workflow_id, body, source=RunSource.API, actor=actor)
    if not result.created:
        response.status_code = 200
    return result
