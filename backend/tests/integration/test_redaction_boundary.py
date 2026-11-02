"""Every persistence path redacts credentials (a security guarantee)."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from agenttape.application.context import AppContext
from agenttape.application.schemas.catalog import PromptTemplateCreate
from agenttape.application.schemas.runs import RunCreate
from agenttape.application.services.approvals import ApprovalService
from agenttape.application.services.exports import ExportService
from agenttape.application.services.prompts import PromptService
from agenttape.application.services.runs import RunService
from agenttape.application.transitions import transition_run
from agenttape.domain.enums import RunStatus, VariableType
from agenttape.domain.errors import ValidationError
from agenttape.domain.prompts import VariableDefinition
from agenttape.infrastructure.db.models import ApprovalRequest, Event, Run
from tests.conftest import demo_profile

FAKE_KEY = "AKIA" + "QQQQWWWWEEEERRRR"
FAKE_GH = "ghp_" + "z" * 36


def test_inputs_and_inline_prompts_are_redacted_with_a_warning(ctx: AppContext) -> None:
    pid = demo_profile(ctx)
    run = (
        RunService(ctx)
        .create_run(RunCreate(agent_profile_id=pid, prompt=f"Deploy with key {FAKE_KEY} please"))
        .run
    )
    detail = RunService(ctx).get_detail(run.id)
    blob = json.dumps(detail.model_dump(mode="json"))
    assert FAKE_KEY not in blob
    assert "[REDACTED:aws_access_key]" in (detail.rendered_prompt or "")
    warnings = RunService(ctx).list_events(run.id, types=["warning"]).items
    assert warnings and "redacted before storage" in warnings[0].summary


def test_secret_reference_variables_keep_their_names(ctx: AppContext) -> None:
    tid = (
        PromptService(ctx)
        .create_template(
            PromptTemplateCreate(
                name="with secret",
                body="Use {{ ci_token }} and password={{ note }}",
                variables=[
                    VariableDefinition(name="ci_token", type=VariableType.SECRET),
                    VariableDefinition(name="note", type=VariableType.MULTILINE),
                ],
            )
        )
        .id
    )
    run = (
        RunService(ctx)
        .create_run(
            RunCreate(
                agent_profile_id=demo_profile(ctx),
                prompt_template_id=tid,
                inputs={"ci_token": "GITHUB_TOKEN", "note": f"also {FAKE_GH}"},
            )
        )
        .run
    )
    detail = RunService(ctx).get_detail(run.id)
    assert detail.inputs["ci_token"] == "GITHUB_TOKEN"
    assert FAKE_GH not in json.dumps(detail.model_dump(mode="json"))
    assert "[secret:GITHUB_TOKEN]" in (detail.rendered_prompt or "")


@pytest.mark.parametrize("body", [f"Use {FAKE_KEY}", "Authorization: Bearer " + "x" * 30])
def test_prompts_reject_credentials(ctx: AppContext, body: str) -> None:
    with pytest.raises(ValidationError, match="looks like a credential"):
        PromptService(ctx).create_template(PromptTemplateCreate(name="leaky", body=body))


def test_prompt_default_values_are_checked(ctx: AppContext) -> None:
    with pytest.raises(ValidationError):
        PromptService(ctx).create_template(
            PromptTemplateCreate(
                name="leaky default",
                body="{{ k }}",
                variables=[VariableDefinition(name="k", default=FAKE_GH)],
            )
        )


def test_outputs_approvals_and_core_updates_are_redacted(ctx: AppContext) -> None:
    run_id = (
        RunService(ctx).create_run(RunCreate(agent_profile_id=demo_profile(ctx), prompt="x")).run.id
    )
    with ctx.session() as session:
        run = session.get(Run, run_id)
        assert run is not None
        run.outputs = {"note": f"key {FAKE_KEY}", "password": "hunter2hunter2"}
        run.notes = f"remember {FAKE_GH}"
        ApprovalService(ctx).request(
            session,
            workspace_id=run.workspace_id,
            title="Push?",
            rationale=f"needs {FAKE_GH}",
            requested_action={"env": {"API_KEY": "plain-value-123"}},
            run_id=run_id,
            resume_handler="none",
        )
    with ctx.session() as session:
        run = session.get(Run, run_id)
        assert run is not None
        transition_run(session, run, RunStatus.CANCELLED, failure_message=f"leaked {FAKE_KEY}")
    with ctx.session() as session:
        stored = session.get(Run, run_id)
        approval = session.scalars(select(ApprovalRequest)).one()
        rows = [
            json.dumps(stored.outputs) if stored else "",
            stored.notes or "" if stored else "",
            stored.failure_message or "" if stored else "",
            approval.rationale or "",
            json.dumps(approval.requested_action),
        ]
        events = " ".join(json.dumps(e.payload) for e in session.scalars(select(Event)))
    joined = " ".join(rows) + events
    for secret in (FAKE_KEY, FAKE_GH, "hunter2hunter2", "plain-value-123"):
        assert secret not in joined
    bundle = json.dumps(ExportService(ctx).run_bundle(run_id))
    assert FAKE_KEY not in bundle and FAKE_GH not in bundle


def test_chunked_body_over_the_cap_is_rejected(tmp_path: object) -> None:
    from pathlib import Path

    from fastapi.testclient import TestClient

    from agenttape.api.app import create_app
    from agenttape.bootstrap import build_context
    from tests.conftest import make_settings

    ctx = build_context(
        make_settings(Path(str(tmp_path)), max_request_body_bytes=100), migrate=True
    )
    try:
        with TestClient(create_app(ctx), base_url="http://127.0.0.1") as c:

            def chunks():  # type: ignore[no-untyped-def]
                for _ in range(10):
                    yield b"x" * 50

            r = c.post(
                "/api/v1/prompts", content=chunks(), headers={"content-type": "application/json"}
            )
            assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"
    finally:
        ctx.close()
