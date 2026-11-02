from __future__ import annotations

import threading
from pathlib import Path

import pytest

from agenttape.application.context import AppContext
from agenttape.application.schemas.catalog import (
    AgentProfileVersionCreate,
    ProjectCreate,
    PromptTemplateCreate,
    PromptVersionCreate,
)
from agenttape.application.schemas.runs import RunCreate
from agenttape.application.services.audit import list_audit
from agenttape.application.services.profiles import AgentProfileService
from agenttape.application.services.projects import ProjectService
from agenttape.application.services.prompts import PromptService
from agenttape.application.services.runs import RunService
from agenttape.domain.enums import RunSource, VariableType
from agenttape.domain.errors import (
    ConflictError,
    IdempotencyConflictError,
    PolicyViolationError,
    ValidationError,
)
from agenttape.domain.prompts import VariableDefinition
from tests.conftest import demo_profile, drive, simple_prompt, start_run


def test_create_run_snapshots_everything(ctx: AppContext) -> None:
    pid = demo_profile(ctx)
    tid = simple_prompt(ctx)
    run = (
        RunService(ctx)
        .create_run(
            RunCreate(agent_profile_id=pid, prompt_template_id=tid, inputs={"thing": "the build"})
        )
        .run
    )
    detail = RunService(ctx).get_detail(run.id)
    snap = detail.snapshot
    assert run.status == "queued" and run.queue_position == 1
    assert snap["prompt"]["body"] == "Please fix {{ thing }}."
    assert snap["prompt"]["rendered"] == "Please fix the build."
    assert snap["agent_profile"]["config"]["scenario"] == "success"
    assert snap["inputs"] == {"thing": "the build"}
    assert snap["environment"]["agenttape_version"]
    assert detail.rendered_prompt == "Please fix the build."


def test_later_edits_never_rewrite_history(ctx: AppContext) -> None:
    pid = demo_profile(ctx)
    tid = simple_prompt(ctx)
    run_id = (
        RunService(ctx).create_run(RunCreate(agent_profile_id=pid, prompt_template_id=tid)).run.id
    )
    PromptService(ctx).create_version(
        tid,
        PromptVersionCreate(
            body="Totally different {{ thing }}", variables=[VariableDefinition(name="thing")]
        ),
    )
    AgentProfileService(ctx).create_version(
        pid, AgentProfileVersionCreate(config={"scenario": "fail"})
    )
    snap = RunService(ctx).get_detail(run_id).snapshot
    assert snap["prompt"]["body"] == "Please fix {{ thing }}." and snap["prompt"]["version"] == 1
    assert snap["agent_profile"]["config"]["scenario"] == "success"
    versions = PromptService(ctx).list_versions(tid)
    assert [v.version for v in versions] == [2, 1]
    assert versions[1].locked_at is not None and versions[0].locked_at is None
    new_run = (
        RunService(ctx)
        .create_run(RunCreate(agent_profile_id=pid, prompt_template_id=tid, inputs={"thing": "x"}))
        .run
    )
    assert RunService(ctx).get_detail(new_run.id).snapshot["prompt"]["version"] == 2


def test_invalid_inputs_are_rejected(ctx: AppContext) -> None:
    pid = demo_profile(ctx)
    tid = (
        PromptService(ctx)
        .create_template(
            PromptTemplateCreate(
                name="typed",
                body="n={{ n }}",
                variables=[VariableDefinition(name="n", type=VariableType.NUMBER)],
            )
        )
        .id
    )
    with pytest.raises(ValidationError) as info:
        RunService(ctx).create_run(
            RunCreate(agent_profile_id=pid, prompt_template_id=tid, inputs={"n": "abc"})
        )
    assert info.value.details["issues"][0]["variable"] == "n"


def test_inline_prompt_cannot_use_variables(ctx: AppContext) -> None:
    pid = demo_profile(ctx)
    with pytest.raises(ValidationError):
        RunService(ctx).create_run(RunCreate(agent_profile_id=pid, prompt="Hi {{ name }}"))


def test_idempotency_key_returns_existing_run(ctx: AppContext) -> None:
    pid = demo_profile(ctx)
    svc = RunService(ctx)
    first = svc.create_run(
        RunCreate(agent_profile_id=pid, prompt="x", idempotency_key="k1", source=RunSource.WEBHOOK)
    )
    again = svc.create_run(
        RunCreate(agent_profile_id=pid, prompt="x", idempotency_key="k1", source=RunSource.WEBHOOK)
    )
    other_source = svc.create_run(
        RunCreate(agent_profile_id=pid, prompt="x", idempotency_key="k1", source=RunSource.CLI)
    )
    assert first.created and not again.created and other_source.created
    assert first.run.id == again.run.id != other_source.run.id


def test_concurrent_idempotent_creates_make_one_run(ctx: AppContext) -> None:
    pid = demo_profile(ctx)
    results: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def create() -> None:
        barrier.wait()
        try:
            r = RunService(ctx).create_run(
                RunCreate(agent_profile_id=pid, prompt="x", idempotency_key="same")
            )
            with lock:
                results.append(r.run.id)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=create) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(set(results)) == 1 and len(results) == 8
    assert RunService(ctx).list_runs().total == 1


def test_cancel_queued_run(ctx: AppContext) -> None:
    pid = demo_profile(ctx)
    run_id = start_run(ctx, pid)
    out = RunService(ctx).cancel_run(run_id, reason="changed my mind")
    assert out.status == "cancelled" and out.failure_category == "cancelled"
    assert ctx.queue.claim("w", 30) is None  # queue item was cancelled too
    assert RunService(ctx).cancel_run(run_id).status == "cancelled"  # idempotent
    retried = RunService(ctx).retry_run(run_id)
    assert retried.status == "queued" and retried.failure_category is None
    assert drive(ctx, run_id).run.status == "succeeded"
    with ctx.session() as session:
        audit = [e.action for e in list_audit(session, run_id=run_id).items]
    assert {"run.create", "run.cancel", "run.retry"} <= set(audit)


def test_cannot_cancel_finished_run(ctx: AppContext) -> None:
    run_id = start_run(ctx, demo_profile(ctx))
    drive(ctx, run_id)
    with pytest.raises(ConflictError):
        RunService(ctx).cancel_run(run_id)
    with pytest.raises(ConflictError):
        RunService(ctx).retry_run(run_id)


def test_list_filters_sort_and_pagination(ctx: AppContext) -> None:
    ok = demo_profile(ctx, "success")
    bad = demo_profile(ctx, "fail")
    ids = [start_run(ctx, ok, title=f"ok {i}") for i in range(3)]
    failed = start_run(ctx, bad, title="broken thing")
    drive(ctx, failed)
    svc = RunService(ctx)
    assert svc.list_runs(status=["failed"]).total == 1
    assert svc.list_runs(search="broken").items[0].id == failed
    assert svc.list_runs(failure_category="validation_failed").total == 1
    page = svc.list_runs(limit=2, offset=0, sort="title", order="asc")
    assert page.total == 4 and len(page.items) == 2 and page.next_offset == 2
    assert [r.title for r in page.items] == ["broken thing", "ok 0"]
    last = svc.list_runs(limit=2, offset=2, sort="title", order="asc")
    assert last.next_offset is None and {r.id for r in last.items} == set(ids[1:])
    with pytest.raises(ValidationError):
        svc.list_runs(sort="nope")


def test_project_requires_allowlisted_path(ctx: AppContext, tmp_path: Path) -> None:
    inside = tmp_path / "repos" / "myrepo"
    inside.mkdir(parents=True)
    project = ProjectService(ctx).create(ProjectCreate(name="Mine", repo_path=str(inside)))
    assert project.repo_path == str(inside.resolve())
    assert project.git_state is not None and project.git_state["is_repo"] is False
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(PolicyViolationError):
        ProjectService(ctx).create(ProjectCreate(name="Nope", repo_path=str(outside)))
    run = (
        RunService(ctx)
        .create_run(
            RunCreate(agent_profile_id=demo_profile(ctx), prompt="x", project_id=project.id)
        )
        .run
    )
    assert RunService(ctx).get_detail(run.id).snapshot["project"]["name"] == "Mine"


def test_profile_validation_before_save(ctx: AppContext) -> None:
    with pytest.raises(ValidationError) as info:
        demo_profile(ctx, "nope")
    assert info.value.details["issues"][0]["field"] == "scenario"
    with pytest.raises(ValidationError):
        demo_profile(ctx, "success", name="bad-eval", evaluators=[{"type": "does_not_exist"}])
    with pytest.raises(ValidationError):
        demo_profile(ctx, "success", name="bad-timeout", timeout_seconds=-1)


def test_idempotency_key_reused_with_a_different_request_conflicts(ctx: AppContext) -> None:
    pid = demo_profile(ctx)
    service = RunService(ctx)
    first = service.create_run(
        RunCreate(agent_profile_id=pid, prompt="first goal", idempotency_key="k1")
    )
    # Same request (a title is presentation only): the original run comes back.
    again = service.create_run(
        RunCreate(agent_profile_id=pid, prompt="first goal", idempotency_key="k1", title="t")
    )
    assert again.created is False and again.run.id == first.run.id
    with pytest.raises(IdempotencyConflictError) as info:
        service.create_run(
            RunCreate(agent_profile_id=pid, prompt="DIFFERENT goal", idempotency_key="k1")
        )
    assert info.value.details["existing_run_id"] == first.run.id
    # Keys are scoped to their source.
    other = service.create_run(
        RunCreate(
            agent_profile_id=pid,
            prompt="DIFFERENT goal",
            idempotency_key="k1",
            source=RunSource.CLI,
        )
    )
    assert other.created is True
