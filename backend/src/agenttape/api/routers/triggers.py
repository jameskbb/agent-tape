"""Webhook trigger management: create (token shown once), update, rotate, deliveries."""

from __future__ import annotations

from fastapi import APIRouter, Response

from agenttape.api.deps import Actor, Ctx, Paging
from agenttape.application.schemas.common import Page
from agenttape.application.schemas.schedules import (
    DeliveryOut,
    MappingTestRequest,
    MappingTestResult,
    TriggerCreate,
    TriggerOut,
    TriggerUpdate,
    TriggerWithToken,
)
from agenttape.application.services.triggers import TriggerService

router = APIRouter(tags=["triggers"])


@router.get("/triggers", response_model=Page[TriggerOut], summary="List webhook triggers")
def list_triggers(ctx: Ctx, paging: Paging) -> Page[TriggerOut]:
    return TriggerService(ctx).list_triggers(limit=paging.limit, offset=paging.offset)


@router.post(
    "/triggers",
    response_model=TriggerWithToken,
    status_code=201,
    summary="Create a trigger. The token is returned once and stored only as a hash.",
)
def create_trigger(body: TriggerCreate, ctx: Ctx, actor: Actor) -> TriggerWithToken:
    return TriggerService(ctx).create(body, actor=actor)


@router.get("/triggers/{trigger_id}", response_model=TriggerOut)
def get_trigger(trigger_id: str, ctx: Ctx) -> TriggerOut:
    return TriggerService(ctx).get(trigger_id)


@router.patch("/triggers/{trigger_id}", response_model=TriggerOut)
def update_trigger(trigger_id: str, body: TriggerUpdate, ctx: Ctx, actor: Actor) -> TriggerOut:
    return TriggerService(ctx).update(trigger_id, body, actor=actor)


@router.delete("/triggers/{trigger_id}", status_code=204)
def delete_trigger(trigger_id: str, ctx: Ctx, actor: Actor) -> Response:
    TriggerService(ctx).delete(trigger_id, actor=actor)
    return Response(status_code=204)


@router.post(
    "/triggers/{trigger_id}/rotate-token",
    response_model=TriggerWithToken,
    summary="Replace the token (the old one stops working immediately)",
)
def rotate_trigger_token(trigger_id: str, ctx: Ctx, actor: Actor) -> TriggerWithToken:
    return TriggerService(ctx).rotate_token(trigger_id, actor=actor)


@router.get(
    "/triggers/{trigger_id}/deliveries",
    response_model=Page[DeliveryOut],
    summary="Delivery log: accepted, duplicate, rejected, error",
)
def list_deliveries(trigger_id: str, ctx: Ctx, paging: Paging) -> Page[DeliveryOut]:
    return TriggerService(ctx).deliveries(trigger_id, limit=paging.limit, offset=paging.offset)


@router.post(
    "/triggers/{trigger_id}/test-mapping",
    response_model=MappingTestResult,
    summary="Apply the input mapping to a sample payload without creating a run",
)
def test_mapping(trigger_id: str, body: MappingTestRequest, ctx: Ctx) -> MappingTestResult:
    return TriggerService(ctx).test_mapping(trigger_id, body.payload)
