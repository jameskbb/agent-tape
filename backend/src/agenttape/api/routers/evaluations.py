"""Evaluator registry and per-run evaluation results."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from agenttape.api.deps import Ctx
from agenttape.application.schemas.runs import EvaluationResultOut
from agenttape.application.services.runs import RunService

router = APIRouter(tags=["evaluations"])


class EvaluatorInfo(BaseModel):
    type: str
    version: str
    display_name: str
    description: str
    deterministic: bool
    config_schema: dict[str, Any]


@router.get("/evaluators", response_model=list[EvaluatorInfo], summary="Registered evaluator types")
def list_evaluators(ctx: Ctx) -> list[EvaluatorInfo]:
    return [
        EvaluatorInfo(
            type=e.type,
            version=e.version,
            display_name=e.display_name or e.type,
            description=e.description,
            deterministic=e.deterministic,
            config_schema=e.config_schema(),
        )
        for e in ctx.evaluators.all()
    ]


@router.get("/runs/{run_id}/evaluations", response_model=list[EvaluationResultOut])
def run_evaluations(run_id: str, ctx: Ctx) -> list[EvaluationResultOut]:
    return RunService(ctx).get_detail(run_id).evaluations
