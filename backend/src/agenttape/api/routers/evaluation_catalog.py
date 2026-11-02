"""Evaluation definitions and suites, ad hoc and rerun evaluations, human scores, comparison."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from agenttape.api.deps import Actor, Ctx, Paging
from agenttape.application.schemas.common import Page
from agenttape.application.schemas.evaluation_catalog import (
    EvaluateRunRequest,
    EvaluationComparison,
    EvaluationDefinitionCreate,
    EvaluationDefinitionOut,
    EvaluationDefinitionUpdate,
    EvaluationDefinitionVersionCreate,
    EvaluationDefinitionVersionOut,
    EvaluationRunResponse,
    EvaluationSuiteCreate,
    EvaluationSuiteOut,
    EvaluationSuiteUpdate,
    EvaluatorTypeInfo,
    HumanScoreCreate,
    RerunRequest,
)
from agenttape.application.schemas.runs import EvaluationResultOut
from agenttape.application.services.evaluation_catalog import EvaluationCatalogService

router = APIRouter(tags=["evaluations"])


@router.get(
    "/evaluator-types",
    response_model=list[EvaluatorTypeInfo],
    summary="Evaluator types with availability (agent-as-judge is unavailable unless configured)",
)
def evaluator_types(ctx: Ctx) -> list[EvaluatorTypeInfo]:
    return EvaluationCatalogService(ctx).evaluator_types()


@router.get("/evaluation-definitions", response_model=Page[EvaluationDefinitionOut])
def list_definitions(
    ctx: Ctx,
    paging: Paging,
    q: Annotated[str | None, Query(max_length=200)] = None,
    include_archived: bool = False,
) -> Page[EvaluationDefinitionOut]:
    return EvaluationCatalogService(ctx).list_definitions(
        search=q, include_archived=include_archived, limit=paging.limit, offset=paging.offset
    )


@router.post("/evaluation-definitions", response_model=EvaluationDefinitionOut, status_code=201)
def create_definition(
    body: EvaluationDefinitionCreate, ctx: Ctx, actor: Actor
) -> EvaluationDefinitionOut:
    return EvaluationCatalogService(ctx).create_definition(body, actor=actor)


@router.get("/evaluation-definitions/{definition_id}", response_model=EvaluationDefinitionOut)
def get_definition(definition_id: str, ctx: Ctx) -> EvaluationDefinitionOut:
    return EvaluationCatalogService(ctx).get_definition(definition_id)


@router.patch("/evaluation-definitions/{definition_id}", response_model=EvaluationDefinitionOut)
def update_definition(
    definition_id: str, body: EvaluationDefinitionUpdate, ctx: Ctx, actor: Actor
) -> EvaluationDefinitionOut:
    return EvaluationCatalogService(ctx).update_definition(definition_id, body, actor=actor)


@router.get(
    "/evaluation-definitions/{definition_id}/versions",
    response_model=list[EvaluationDefinitionVersionOut],
)
def list_definition_versions(definition_id: str, ctx: Ctx) -> list[EvaluationDefinitionVersionOut]:
    return EvaluationCatalogService(ctx).list_definition_versions(definition_id)


@router.post(
    "/evaluation-definitions/{definition_id}/versions",
    response_model=EvaluationDefinitionVersionOut,
    status_code=201,
)
def create_definition_version(
    definition_id: str, body: EvaluationDefinitionVersionCreate, ctx: Ctx, actor: Actor
) -> EvaluationDefinitionVersionOut:
    return EvaluationCatalogService(ctx).create_definition_version(definition_id, body, actor=actor)


@router.get("/evaluation-suites", response_model=Page[EvaluationSuiteOut])
def list_suites(ctx: Ctx, paging: Paging) -> Page[EvaluationSuiteOut]:
    return EvaluationCatalogService(ctx).list_suites(limit=paging.limit, offset=paging.offset)


@router.post("/evaluation-suites", response_model=EvaluationSuiteOut, status_code=201)
def create_suite(body: EvaluationSuiteCreate, ctx: Ctx, actor: Actor) -> EvaluationSuiteOut:
    return EvaluationCatalogService(ctx).create_suite(body, actor=actor)


@router.get("/evaluation-suites/{suite_id}", response_model=EvaluationSuiteOut)
def get_suite(suite_id: str, ctx: Ctx) -> EvaluationSuiteOut:
    return EvaluationCatalogService(ctx).get_suite(suite_id)


@router.patch("/evaluation-suites/{suite_id}", response_model=EvaluationSuiteOut)
def update_suite(
    suite_id: str, body: EvaluationSuiteUpdate, ctx: Ctx, actor: Actor
) -> EvaluationSuiteOut:
    return EvaluationCatalogService(ctx).update_suite(suite_id, body, actor=actor)


@router.post(
    "/runs/{run_id}/evaluations/run",
    response_model=EvaluationRunResponse,
    summary="Evaluate a run now with definitions, a suite, or inline evaluators",
)
async def evaluate_run(
    run_id: str, body: EvaluateRunRequest, ctx: Ctx, actor: Actor
) -> EvaluationRunResponse:
    return await EvaluationCatalogService(ctx).evaluate_run(run_id, body, actor=actor)


@router.post(
    "/runs/{run_id}/evaluations/rerun",
    response_model=EvaluationRunResponse,
    summary="Rerun deterministic evaluations against the run's recorded data",
)
async def rerun_evaluations(
    run_id: str, body: RerunRequest, ctx: Ctx, actor: Actor
) -> EvaluationRunResponse:
    return await EvaluationCatalogService(ctx).rerun(run_id, body.result_ids, actor=actor)


@router.post(
    "/runs/{run_id}/evaluations/human",
    response_model=EvaluationResultOut,
    status_code=201,
    summary="Record a person's score and notes",
)
def human_score(run_id: str, body: HumanScoreCreate, ctx: Ctx, actor: Actor) -> EvaluationResultOut:
    return EvaluationCatalogService(ctx).human_score(run_id, body, actor=actor)


@router.get(
    "/evaluations/compare",
    response_model=EvaluationComparison,
    summary="Compare evaluation results across runs (2-6 runs)",
)
def compare(
    ctx: Ctx, run_id: Annotated[list[str], Query(min_length=2, max_length=6)]
) -> EvaluationComparison:
    return EvaluationCatalogService(ctx).compare(run_id)
