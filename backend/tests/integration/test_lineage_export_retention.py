"""Replay, fork, comparison, sanitized export, and retention (API, services, CLI)."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from agenttape.application.context import AppContext
from agenttape.application.schemas.catalog import PromptVersionCreate
from agenttape.application.schemas.git import ForkRequest, RetentionExecute, RetentionSettings
from agenttape.application.schemas.runs import RunCreate
from agenttape.application.services.artifacts import ArtifactService
from agenttape.application.services.comparison import ComparisonService
from agenttape.application.services.exports import ExportService
from agenttape.application.services.lineage import LineageService
from agenttape.application.services.projects import ProjectService
from agenttape.application.services.prompts import PromptService
from agenttape.application.services.retention import RetentionService
from agenttape.application.services.runs import RunService
from agenttape.domain.enums import RunSource
from agenttape.domain.errors import ConflictError, ValidationError
from agenttape.domain.prompts import VariableDefinition
from agenttape.infrastructure.db.models import Artifact, AuditEntry, Event, Run, SecretReference
from agenttape.security.redaction import Redactor
from tests.conftest import demo_profile, drive, simple_prompt
from tests.integration.test_context_evidence import run_with

THING = VariableDefinition(name="thing", default="bugs")


def _original(ctx: AppContext, tag: str = "a") -> tuple[str, str, str]:
    profile = demo_profile(ctx, name=f"demo {tag}")
    prompt = simple_prompt(ctx, name=f"Fix things {tag}")
    run = (
        RunService(ctx)
        .create_run(
            RunCreate(
                agent_profile_id=profile,
                prompt_template_id=prompt,
                inputs={"thing": "the parser"},
                source=RunSource.UI,
            )
        )
        .run
    )
    drive(ctx, run.id)
    return run.id, profile, prompt


def test_replay_pins_versions_and_inputs(ctx: AppContext) -> None:
    original_id, _profile, prompt = _original(ctx)
    # A newer prompt version exists now; replay must still use the original one.
    PromptService(ctx).create_version(
        prompt, PromptVersionCreate(body="Changed {{ thing }}.", variables=[THING])
    )
    result = LineageService(ctx).replay(original_id, actor="tester")
    assert result.result.created
    new = RunService(ctx).get_detail(result.result.run.id)
    old = RunService(ctx).get_detail(original_id)
    assert new.run.source == "replay" and new.run.parent_run_id == original_id
    assert new.run.parent_relation == "replay"
    assert new.run.prompt_version_id == old.run.prompt_version_id
    assert new.run.agent_profile_version_id == old.run.agent_profile_version_id
    assert new.inputs == old.inputs and new.rendered_prompt == old.rendered_prompt
    assert new.snapshot["replay"]["of_run_id"] == original_id
    assert drive(ctx, new.run.id).run.status == "succeeded"
    lineage = LineageService(ctx).lineage(original_id)
    assert [c.id for c in lineage.children] == [new.run.id]
    assert lineage.children[0].relation == "replay"
    assert LineageService(ctx).lineage(new.run.id).parent.id == original_id  # type: ignore[union-attr]


def test_fork_changes_variables_and_prompt_version(ctx: AppContext) -> None:
    original_id, _profile, prompt = _original(ctx)
    v2 = PromptService(ctx).create_version(
        prompt, PromptVersionCreate(body="Now fix {{ thing }}!", variables=[THING])
    )
    with pytest.raises(ValidationError):
        LineageService(ctx).fork(original_id, ForkRequest(inputs={"thing": "the parser"}))
    result = LineageService(ctx).fork(
        original_id,
        ForkRequest(prompt_version_id=v2.id, inputs={"thing": "the lexer"}),
        actor="tester",
    )
    fields = {c["field"] for c in result.changes}
    assert fields == {"prompt_version", "inputs.thing"}
    new = RunService(ctx).get_detail(result.result.run.id)
    assert new.run.source == "fork" and new.run.parent_relation == "fork"
    assert new.run.prompt_version_id == v2.id and new.inputs["thing"] == "the lexer"
    assert new.rendered_prompt == "Now fix the lexer!"
    drive(ctx, new.run.id)

    cmp = ComparisonService(ctx).compare(original_id, new.run.id)
    d = cmp.differences
    assert cmp.relation == "b_is_fork_of_a"
    assert d["inputs"] == [{"key": "thing", "a": "the parser", "b": "the lexer"}]
    assert d["prompt"]["same_template"] and not d["prompt"]["same_version"]
    assert d["prompt"]["a_version"] == 1 and d["prompt"]["b_version"] == 2
    assert d["usage"]["a_status"] == "simulated"  # the Demo Agent labels its usage
    assert {r["key"] for r in d["event_types"]} >= {"lifecycle", "tool_call"}
    assert all(not e["changed"] for e in d["evaluations"])
    assert cmp.a.git["files"] == cmp.b.git["files"] == 3


def test_fork_to_another_profile_and_bad_versions(ctx: AppContext) -> None:
    original_id, _profile, _prompt = _original(ctx)
    other = demo_profile(ctx, "fail", name="other")
    result = LineageService(ctx).fork(original_id, ForkRequest(agent_profile_id=other))
    assert result.changes[0]["field"] == "agent_profile"
    assert drive(ctx, result.result.run.id).run.status == "failed"
    other_prompt = simple_prompt(ctx, name="Other prompt")
    foreign = PromptService(ctx).list_versions(other_prompt)[0]
    with pytest.raises(ValidationError):
        LineageService(ctx).fork(original_id, ForkRequest(prompt_version_id=foreign.id))


def test_replay_warns_about_drift(ctx: AppContext) -> None:
    from agenttape.application.schemas.catalog import ProjectSettings, ProjectUpdate
    from agenttape.application.services.demo import DemoService

    report = DemoService(ctx).seed()
    parent_id = report.data["compare_runs"][0]
    project_id = report.data["isolated_project_id"]
    assert LineageService(ctx).drift(parent_id) == []
    current = ProjectService(ctx).get(project_id).settings
    changed = ProjectSettings.model_validate({**current, "timeout_seconds": 42})
    ProjectService(ctx).update(project_id, ProjectUpdate(settings=changed))
    result = LineageService(ctx).replay(parent_id)
    assert [d.field for d in result.drift] == ["project.settings"]
    assert "timeout_seconds" in result.drift[0].message
    events = RunService(ctx).list_events(result.result.run.id, types=["warning"]).items
    assert any("Environment differs" in e.summary for e in events)
    assert drive(ctx, result.result.run.id).run.status == "succeeded"


def test_replay_of_unsupported_kind_is_refused(ctx: AppContext) -> None:
    original_id, _p, _t = _original(ctx)
    with ctx.session() as session:
        session.execute(update(Run).where(Run.id == original_id).values(kind="workflow"))
    with pytest.raises(ConflictError):
        LineageService(ctx).replay(original_id)
    assert not LineageService(ctx).lineage(original_id).options.replayable


def test_lineage_api_and_compare_endpoint(client: TestClient, ctx: AppContext) -> None:
    original_id, _p, _t = _original(ctx)
    lineage = client.get(f"/api/v1/runs/{original_id}/lineage").json()
    assert lineage["options"]["replayable"] and lineage["options"]["inputs"]["thing"]
    replay = client.post(f"/api/v1/runs/{original_id}/replay", json={})
    assert replay.status_code == 201
    new_id = replay.json()["result"]["run"]["id"]
    fork = client.post(f"/api/v1/runs/{original_id}/fork", json={"inputs": {"thing": "x"}})
    assert fork.status_code == 201 and fork.json()["changes"][0]["field"] == "inputs.thing"
    drive(ctx, new_id)
    cmp = client.get(f"/api/v1/runs/{original_id}/compare/{new_id}")
    assert cmp.status_code == 200
    body = cmp.json()
    assert body["relation"] == "b_is_replay_of_a" and body["differences"]["inputs"] == []
    assert client.get(f"/api/v1/runs/{original_id}/compare/nope").status_code == 404
    assert client.post("/api/v1/runs/nope/replay", json={}).status_code == 404


# --------------------------------------------------------------------------- export


def test_export_carries_findings_with_citations_verdicts_and_confirmation(
    client: TestClient, ctx: AppContext, repo_project: str
) -> None:
    quote = "def greet():\n    return 'hi'"
    run_id = run_with(
        ctx,
        repo_project,
        [
            {
                "type": "finding",
                "payload": {
                    "id": "F1",
                    "kind": "claim",
                    "text": "greet() returns a fixed string",
                    "links": [
                        {"path": "src/greeter.py", "line_start": 1, "line_end": 2, "quote": quote}
                    ],
                },
            }
        ],
    )
    bundle = ExportService(ctx).run_bundle(run_id)
    [finding] = [f for f in bundle["findings"] if f["key"] == "F1"]
    assert finding["kind"] == "claim"
    assert finding["text"] == "greet() returns a fixed string"
    [citation] = finding["citations"]
    assert citation["verdict"]["verdict"] is not None
    assert citation["checks"] == 1
    assert citation["reproduction"] is None
    assert finding["confirmation"] is None
    assert bundle["challenges"] == []

    api_json = client.get(f"/api/v1/runs/{run_id}/export").json()
    assert api_json["findings"] == bundle["findings"]
    assert api_json["challenges"] == bundle["challenges"]

    archive = ExportService(ctx).run_bundle_archive(run_id)
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        zipped = json.loads(zf.read("bundle.json"))
    assert zipped["findings"][0]["key"] == "F1"
    assert zipped["challenges"] == []


def test_export_of_a_run_with_no_findings_has_empty_lists(
    ctx: AppContext, client: TestClient
) -> None:
    original_id, _p, _t = _original(ctx)
    bundle = ExportService(ctx).run_bundle(original_id)
    assert bundle["findings"] == []
    assert bundle["challenges"] == []
    api_json = client.get(f"/api/v1/runs/{original_id}/export").json()
    assert api_json["findings"] == [] and api_json["challenges"] == []


def test_export_masks_host_paths(ctx: AppContext, repo_project: str) -> None:
    """A host path is masked wherever it appears: in a finding's text and in run notes."""
    data_dir = str(ctx.settings.data_dir)
    run_id = run_with(
        ctx,
        repo_project,
        [
            {
                "type": "finding",
                "payload": {
                    "id": "F1",
                    "kind": "claim",
                    "text": f"see {data_dir}/logs/x.log for the trace",
                },
            }
        ],
    )
    with ctx.session() as session:
        session.execute(
            update(Run).where(Run.id == run_id).values(notes=f"see {data_dir}/logs/y.log")
        )
    text = json.dumps(ExportService(ctx).run_bundle(run_id))
    assert data_dir not in text
    assert "$AGENTTAPE_DATA_DIR/logs/x.log" in text
    assert "$AGENTTAPE_DATA_DIR/logs/y.log" in text


def test_export_lists_a_challenge_labelled_model_judgment(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    # Not review_injection.F_UP: this F-UP cites the shared test repository's greeter.py
    # (lines 1-2), not the demo repository's GREETER_V1.
    quote = "def greet():\n    return 'hi'"
    target_run_id = run_with(
        ctx,
        repo_project,
        [
            {
                "type": "finding",
                "payload": {
                    "id": "F-UP",
                    "kind": "claim",
                    "text": "greet() returns a fixed string",
                    "links": [
                        {"path": "src/greeter.py", "line_start": 1, "line_end": 2, "quote": quote}
                    ],
                },
            }
        ],
    )
    reviewer_profile_id = demo_profile(ctx, "finding_review")
    resp = client.post(
        f"/api/v1/runs/{target_run_id}/challenge",
        json={"agent_profile_id": reviewer_profile_id},
    )
    assert resp.status_code == 201, resp.text
    child_id = resp.json()["run"]["id"]
    drive(ctx, child_id)

    bundle = ExportService(ctx).run_bundle(target_run_id)
    assert len(bundle["challenges"]) == 1
    [challenge] = bundle["challenges"]
    assert challenge["judgment"] == "model judgment"
    assert challenge["counts_as_proof"] is False


def test_export_never_contains_a_planted_secret(
    client: TestClient, ctx: AppContext, repo_project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "planted-" + "secret-" + "value-7731"
    monkeypatch.setenv("DEPLOY_KEY_VALUE", secret)
    # A finding recorded before the secret reference existed keeps the raw value in its row.
    original_id = run_with(
        ctx,
        repo_project,
        [
            {
                "type": "finding",
                "payload": {"id": "F1", "kind": "claim", "text": f"the key is {secret}"},
            }
        ],
    )
    with ctx.session() as session:
        ws = session.scalars(select(Run.workspace_id).where(Run.id == original_id)).one()
        session.add(
            SecretReference(
                workspace_id=ws, name="DEPLOY_KEY", provider="env", locator="DEPLOY_KEY_VALUE"
            )
        )
        # Plant the secret where redaction on write would normally have caught it: a raw
        # event row, a text artifact stored with a no-op redactor, and run notes.
        session.add(
            Event(
                run_id=original_id,
                sequence=9999,
                ts=ctx.clock.now(),
                monotonic_ms=0,
                type="stdout",
                source="test",
                severity="info",
                summary=f"leaked {secret}",
                payload={"text": f"key={secret}"},
            )
        )
        ArtifactService(ctx).store(
            session,
            run_id=original_id,
            name="leak.txt",
            data=f"the key is {secret}\n".encode(),
            media_type="text/plain",
            redactor=Redactor(),
        )
        session.execute(update(Run).where(Run.id == original_id).values(notes=f"note {secret}"))
    with ctx.session() as session:
        stored = session.scalars(select(Artifact).where(Artifact.name == "leak.txt")).one()
        with ctx.artifact_store.open(stored.storage_path) as fh:
            assert secret in fh.read().decode()  # the planted value really is on disk

    bundle = ExportService(ctx).run_bundle(original_id)
    text = json.dumps(bundle)
    assert secret not in text and "[REDACTED:secret]" in text
    assert any(a["name"] == "leak.txt" for a in bundle["artifacts"])
    assert any(f["key"] == "F1" for f in bundle["findings"])

    archive = ExportService(ctx).run_bundle_archive(original_id)
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        names = zf.namelist()
        assert "bundle.json" in names and any(n.endswith("leak.txt") for n in names)
        for name in names:
            assert secret not in zf.read(name).decode("utf-8", errors="replace"), name
    assert secret.encode() not in archive  # also not in any compressed-away form header

    api_zip = client.get(f"/api/v1/runs/{original_id}/export/archive")
    assert api_zip.status_code == 200 and api_zip.headers["content-type"] == "application/zip"
    api_json = client.get(f"/api/v1/runs/{original_id}/export")
    assert secret not in api_json.text


# --------------------------------------------------------------------------- retention


def _age(ctx: AppContext, run_id: str, days: int) -> None:
    when = ctx.clock.now() - timedelta(days=days)
    with ctx.session() as session:
        session.execute(
            update(Run).where(Run.id == run_id).values(finished_at=when, created_at=when)
        )


def test_retention_preview_then_execute(ctx: AppContext, client: TestClient) -> None:
    old_id, _p, _t = _original(ctx)
    keep_id, _p2, _t2 = _original(ctx, "b")
    new_id, _p3, _t3 = _original(ctx, "c")
    _age(ctx, old_id, 40)
    _age(ctx, keep_id, 40)
    _age(ctx, new_id, 1)
    RunService(ctx).update_notes(keep_id, notes=None, tags=["keep"])
    orphan = ctx.settings.logs_dir / "00000000-0000-4000-8000-000000000000"
    orphan.mkdir(parents=True)
    (orphan / "attempt-1.stdout.log").write_text("x" * 100)
    outside = ctx.settings.data_dir.parent / "not-agenttape"
    outside.mkdir()

    service = RetentionService(ctx)
    plan = service.preview(RetentionSettings(max_age_days=30))
    assert [i.run_id for i in plan.runs] == [old_id]
    item = plan.runs[0]
    assert item.artifact_bytes > 0 and item.log_bytes > 0 and not item.blocked
    assert [o.path for o in plan.orphans] == [str(orphan)] and plan.orphans[0].bytes == 100
    assert plan.total_bytes == item.total_bytes + 100
    # Preview deletes nothing.
    assert RunService(ctx).get_run(old_id) and orphan.exists()

    service.update_settings(RetentionSettings(max_age_days=30), actor="tester")
    result = service.execute(
        RetentionExecute(
            run_ids=[old_id, new_id, keep_id], orphan_paths=[str(orphan), str(outside)]
        ),
        actor="tester",
    )
    assert result.deleted_runs == [old_id]
    skipped = {s.get("run_id") or s.get("path") for s in result.skipped}
    assert skipped == {new_id, keep_id, str(outside)}
    assert not orphan.exists() and outside.exists()
    assert not (ctx.settings.artifacts_dir / "runs" / old_id).exists()
    assert not (ctx.settings.logs_dir / old_id).exists()
    assert client.get(f"/api/v1/runs/{old_id}").status_code == 404
    assert client.get(f"/api/v1/runs/{new_id}").status_code == 200
    with ctx.session() as session:
        actions = set(session.scalars(select(AuditEntry.action)))
    assert {"retention.cleanup", "retention.settings.update"} <= actions


def test_retention_blocks_dirty_worktrees_and_api(client: TestClient, ctx: AppContext) -> None:
    from agenttape.application.services.demo import DemoService

    report = DemoService(ctx).seed()
    parent_id = report.data["compare_runs"][0]
    _age(ctx, parent_id, 90)
    settings = client.put(
        "/api/v1/retention/settings", json={"max_age_days": 30, "include_orphans": False}
    ).json()
    assert settings["max_age_days"] == 30
    preview = client.post("/api/v1/retention/preview").json()
    item = next(i for i in preview["runs"] if i["run_id"] == parent_id)
    assert item["blocked"] and "uncommitted" in item["blocked"] and item["worktree_bytes"] > 0
    executed = client.post(
        "/api/v1/retention/execute", json={"run_ids": [parent_id], "orphan_paths": []}
    ).json()
    assert executed["deleted_runs"] == [] and executed["skipped"][0]["run_id"] == parent_id
    worktree = Path(item["worktree_path"])
    assert worktree.is_dir()  # uncommitted demo changes were not lost
    bad = client.put("/api/v1/retention/settings", json={"statuses": ["running"]})
    assert bad.status_code == 422
