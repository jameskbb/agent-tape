"""Workflow runs can be replayed with the same immutable version and inputs."""

from __future__ import annotations

import pytest

from agenttape.application.context import AppContext
from agenttape.application.schemas.git import ForkRequest
from agenttape.application.services.demo import DemoService
from agenttape.application.services.lineage import LineageService
from agenttape.application.services.runs import RunService
from agenttape.domain.errors import ConflictError
from tests.conftest import drive


def test_replay_a_workflow_run(ctx: AppContext) -> None:
    report = DemoService(ctx).seed(include_runs=False)
    from agenttape.application.schemas.workflows import WorkflowRunCreate
    from agenttape.application.services.workflows import WorkflowService

    workflow_id = report.data["workflow_id"]
    original = (
        WorkflowService(ctx)
        .start_run(workflow_id, WorkflowRunCreate(inputs={"goal": "Reject empty names."}))
        .run
    )
    assert drive(ctx, original.id, timeout=60).run.status == "waiting_for_approval"

    options = LineageService(ctx).lineage(original.id).options
    assert options.replayable and not options.forkable and options.fork_reason

    replay = LineageService(ctx).replay(original.id).result.run
    assert replay.kind == "workflow" and replay.parent_run_id == original.id
    assert replay.parent_relation == "replay" and replay.source == "replay"
    detail = RunService(ctx).get_detail(replay.id)
    first = RunService(ctx).get_detail(original.id)
    assert detail.snapshot["workflow"]["version_id"] == first.snapshot["workflow"]["version_id"]
    assert detail.inputs == first.inputs
    assert drive(ctx, replay.id, timeout=60).run.status == "waiting_for_approval"

    with pytest.raises(ConflictError):
        LineageService(ctx).fork(original.id, ForkRequest(inputs={"goal": "other"}))
