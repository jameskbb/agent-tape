"""Security review fixes through the real engine: workflow command-node option injection,
project validation commands under the V1 policy, secret variables on the workflow trigger
path, and generic-command's argv prompt mode with secrets."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agenttape.application.context import AppContext
from agenttape.application.schemas.catalog import (
    AgentProfileCreate,
    ProjectCreate,
    ProjectSettings,
    PromptTemplateCreate,
)
from agenttape.application.schemas.runs import RunCreate
from agenttape.application.schemas.workflows import WorkflowCreate, WorkflowRunCreate
from agenttape.application.services.approvals import ApprovalService
from agenttape.application.services.profiles import AgentProfileService
from agenttape.application.services.projects import ProjectService
from agenttape.application.services.prompts import PromptService
from agenttape.application.services.runs import RunService
from agenttape.application.services.workflows import WorkflowService
from agenttape.domain.enums import RunStatus, VariableType
from agenttape.domain.prompts import VariableDefinition
from agenttape.infrastructure.db.models import EvaluationResult, Project
from tests.conftest import demo_profile, drive, event_types, start_run
from tests.integration.test_git_isolation import EditAdapter, make_repo
from tests.integration.test_process_adapters import make_exe

# --------------------------------------------------------------------------- finding 1


def _workflow_with_command(ctx: AppContext, argv: list[str], name: str) -> str:
    definition = {
        "inputs": [{"name": "ref"}],
        "nodes": [{"key": "cmd", "type": "command", "config": {"argv": argv}}],
    }
    return WorkflowService(ctx).create(WorkflowCreate(name=name, definition=definition)).id


@pytest.mark.parametrize(
    "ref",
    [
        "--output=/tmp/agenttape-injected",
        "--open-files-in-pager=touch /tmp/agenttape-injected",
        "-Otouch",
    ],
)
def test_command_node_refuses_a_template_value_that_starts_an_option(
    ctx: AppContext, ref: str
) -> None:
    wf = _workflow_with_command(ctx, ["git", "log", "{{ inputs.ref }}"], f"inject {ref[:8]}")
    run_id = WorkflowService(ctx).start_run(wf, WorkflowRunCreate(inputs={"ref": ref})).run.id
    detail = drive(ctx, run_id)
    assert detail.run.status == RunStatus.FAILED
    step = next(s for s in detail.steps if s.node_key == "cmd")
    assert step.failure_category == "policy_violation"
    assert "cannot start a command-line option" in (step.failure_message or "")
    assert ApprovalService(ctx).list_approvals(run_id=run_id).total == 0


def test_command_node_allows_an_author_written_option_prefix(ctx: AppContext) -> None:
    wf = _workflow_with_command(ctx, ["echo", "--value={{ inputs.ref }}"], "prefixed")
    run_id = WorkflowService(ctx).start_run(wf, WorkflowRunCreate(inputs={"ref": "-x"})).run.id
    detail = drive(ctx, run_id)
    assert detail.run.status == RunStatus.SUCCEEDED, detail.run.failure_message
    step = next(s for s in detail.steps if s.node_key == "cmd")
    assert (step.outputs or {})["stdout"] == "--value=-x"


# --------------------------------------------------------------------------- finding 3


def test_denied_validation_commands_are_refused_and_recorded(ctx: AppContext) -> None:
    ctx.adapters.register(EditAdapter())
    editor = (
        AgentProfileService(ctx)
        .create(
            AgentProfileCreate(
                name="editor",
                adapter_type="test_edit",
                config={"code": "open('src/app.py', 'a').write('x = 1\\n')"},
            )
        )
        .id
    )
    repo = make_repo(Path(ctx.settings.allowed_roots[0]))
    project_id = (
        ProjectService(ctx)
        .create(
            ProjectCreate(
                name="App",
                repo_path=str(repo),
                settings=ProjectSettings(
                    git_isolation=True,
                    validation_commands=[[sys.executable, "-c", "print('ok')"]],
                ),
            )
        )
        .id
    )
    # Settings saved before this check existed (the API now refuses them at save time).
    with ctx.session() as session:
        row = session.get(Project, project_id)
        assert row is not None
        row.settings = {
            **dict(row.settings or {}),
            "validation_commands": [
                [sys.executable, "-c", "print('ok')"],
                ["git", "push", "origin", "HEAD:main"],
                ["env", "-u", "pytest", "git", "push", "origin", "HEAD:main"],
                ["git", "commit", "-am", "sneaky"],
            ],
        }
    detail = drive(ctx, start_run(ctx, editor, project_id=project_id))
    assert detail.run.status == "succeeded", detail.run.failure_message
    with ctx.session() as session:
        evals = session.scalars(
            select(EvaluationResult).where(EvaluationResult.run_id == detail.run.id)
        ).all()
        by_argv = {tuple(e.evidence["argv"]): e for e in evals}
    assert by_argv[(sys.executable, "-c", "print('ok')")].status == "pass"
    for argv in (
        ("git", "push", "origin", "HEAD:main"),
        ("env", "-u", "pytest", "git", "push", "origin", "HEAD:main"),
        ("git", "commit", "-am", "sneaky"),
    ):
        refused = by_argv[argv]
        assert refused.status == "error" and refused.explanation.startswith("Not run:")
        assert "exit_code" not in refused.evidence  # it never ran
    assert "validation_refused" in json.dumps(
        [e.payload for e in RunService(ctx).list_events(detail.run.id, limit=1000).items]
    )


# --------------------------------------------------------------------------- finding 5


def _secret_prompt(ctx: AppContext) -> str:
    return (
        PromptService(ctx)
        .create_template(
            PromptTemplateCreate(
                name="Uses a token",
                body="Deploy with {{ token }}.",
                variables=[
                    VariableDefinition(
                        name="token", type=VariableType.SECRET, default="DEMO_CI_TOKEN"
                    )
                ],
            )
        )
        .id
    )


def _agent_workflow(ctx: AppContext, token_input: Any) -> str:
    node: dict[str, Any] = {
        "key": "agent",
        "type": "agent_run",
        "config": {
            "agent_profile_id": demo_profile(ctx),
            "prompt_template_id": _secret_prompt(ctx),
        },
    }
    if token_input is not None:
        node["inputs"] = {"token": token_input}
    definition = {"inputs": [{"name": "which", "required": False}], "nodes": [node]}
    return WorkflowService(ctx).create(WorkflowCreate(name="wf", definition=definition)).id


def test_workflow_trigger_cannot_let_a_payload_choose_a_secret(
    client: TestClient, ctx: AppContext
) -> None:
    wf = _agent_workflow(ctx, "{{ inputs.which }}")
    resp = client.post(
        "/api/v1/triggers",
        json={
            "name": "wf hook",
            "target_type": "workflow",
            "workflow_id": wf,
            "input_mapping": {"which": "$.secret_name"},
        },
    )
    assert resp.status_code == 422, resp.text
    assert "secret" in resp.json()["error"]["message"]


def test_workflow_edited_after_the_trigger_is_refused_at_delivery(
    client: TestClient, ctx: AppContext
) -> None:
    wf = _agent_workflow(ctx, "DEMO_CI_TOKEN")  # a fixed name is fine
    resp = client.post(
        "/api/v1/triggers",
        json={
            "name": "wf hook",
            "target_type": "workflow",
            "workflow_id": wf,
            "input_mapping": {"which": "$.secret_name"},
        },
    )
    assert resp.status_code == 201, resp.text
    trigger_id, token = resp.json()["trigger"]["id"], resp.json()["token"]
    # A later version wires the payload into the secret variable.
    latest = WorkflowService(ctx).get(wf).latest
    assert latest is not None
    definition = json.loads(json.dumps(latest.definition))
    definition["nodes"][0]["inputs"] = {"token": "{{ inputs.which }}"}
    edit = client.post(f"/api/v1/workflows/{wf}/versions", json={"definition": definition})
    assert edit.status_code == 201, edit.text
    delivery = client.post(
        f"/api/v1/webhooks/{trigger_id}",
        content=json.dumps({"secret_name": "AWS_SECRET_ACCESS_KEY"}),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": "k-1",
        },
    )
    assert delivery.status_code == 422, delivery.text
    assert "secret" in delivery.text


# --------------------------------------------------------------------------- finding 6


def test_generic_command_arg_mode_refuses_a_prompt_with_secrets(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GC_ARG_SECRET", "arg-" + "s3cr3t-" + "1234")
    exe = make_exe(ctx, "argtool", "import sys\nprint(sys.argv[1:])\n")
    pid = (
        AgentProfileService(ctx)
        .create(
            AgentProfileCreate(
                name="argmode",
                adapter_type="generic-command",
                config={"executable": str(exe), "prompt_mode": "arg"},
            )
        )
        .id
    )
    run_id = (
        RunService(ctx)
        .create_run(
            RunCreate(agent_profile_id=pid, prompt="Use {{ secret:GC_ARG_SECRET }} please.")
        )
        .run.id
    )
    detail = drive(ctx, run_id)
    assert detail.run.status == "failed"
    assert detail.run.failure_category == "config_error"
    assert "prompt_mode 'arg'" in (detail.run.failure_message or "")
    assert "command" not in event_types(ctx, run_id)
    # Without secrets, arg mode still works.
    plain = drive(ctx, start_run(ctx, pid))
    assert plain.run.status == "succeeded", plain.run.failure_message


def test_agent_run_refuses_a_secret_chosen_by_another_nodes_output(ctx: AppContext) -> None:
    # A command's (or agent's) output must never pick which secret the next agent receives.
    definition = {
        "nodes": [
            {"key": "pick", "type": "command", "config": {"argv": ["echo", "AWS_SECRET"]}},
            {
                "key": "agent",
                "type": "agent_run",
                "config": {
                    "agent_profile_id": demo_profile(ctx),
                    "prompt_template_id": _secret_prompt(ctx),
                },
                "inputs": {"token": "{{ nodes.pick.outputs.stdout }}"},
            },
        ],
        "edges": [{"from": "pick", "to": "agent"}],
    }
    wf = WorkflowService(ctx).create(WorkflowCreate(name="tainted", definition=definition)).id
    run_id = WorkflowService(ctx).start_run(wf, WorkflowRunCreate()).run.id
    detail = drive(ctx, run_id)
    step = next(s for s in detail.steps if s.node_key == "agent")
    assert step.failure_category == "policy_violation", step.failure_message
    assert "secret variable 'token'" in (step.failure_message or "")
