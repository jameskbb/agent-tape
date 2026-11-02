"""Library API: prompt library, profiles, secret references, projects, onboarding."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agenttape.application.context import AppContext
from agenttape.application.schemas.catalog import PromptVersionCreate
from agenttape.application.schemas.library import PromptVersionAmend
from agenttape.application.services.prompt_library import (
    ImmutableVersionError,
    PromptLibraryService,
)
from agenttape.application.services.prompts import PromptService
from agenttape.domain.prompts import VariableDefinition
from tests.conftest import demo_profile, drive, simple_prompt

API = "/api/v1"

PROMPT: dict[str, Any] = {
    "name": "Summarize a module",
    "description": "Explain one module",
    "tags": ["docs", "review"],
    "owner": "platform team",
    "body": "Summarize {{ module }} in {{ words }} words. Key: {{ secret:DOCS_TOKEN }}",
    "variables": [
        {"name": "module", "type": "path", "default": "src/app.py"},
        {
            "name": "words",
            "type": "number",
            "default": 100,
            "validation": {"minimum": 10, "maximum": 500},
        },
    ],
}


def create_prompt(client: TestClient, **overrides: Any) -> dict[str, Any]:
    res = client.post(f"{API}/prompts", json={**PROMPT, **overrides})
    assert res.status_code == 201, res.text
    data: dict[str, Any] = res.json()
    return data


# --------------------------------------------------------------------------- prompts


def test_prompt_versions_history_and_diff(client: TestClient) -> None:
    p = create_prompt(client)
    tid = p["id"]
    same = {"body": PROMPT["body"], "variables": PROMPT["variables"]}
    res = client.post(f"{API}/prompts/{tid}/versions/save", json=same)
    assert res.status_code == 409 and "Nothing changed" in res.json()["error"]["message"]
    v2 = {
        "body": "Summarize {{ module }} briefly in {{ words }} words for {{ audience }}.",
        "variables": [
            *PROMPT["variables"],
            {"name": "audience", "type": "enum", "options": ["devs", "ops"], "default": "devs"},
        ],
        "notes": "audience",
    }
    res = client.post(f"{API}/prompts/{tid}/versions/save", json=v2)
    assert res.status_code == 201 and res.json()["version"] == 2
    versions = client.get(f"{API}/prompts/{tid}/versions").json()
    assert [v["version"] for v in versions] == [2, 1]
    diff = client.get(f"{API}/prompts/{tid}/diff", params={"from": 1, "to": 2}).json()
    assert diff["variables_added"] == ["audience"]
    assert diff["body_changed"] is True
    ops = {line["op"] for line in diff["lines"]}
    assert ops == {"add", "remove"}
    assert "+Summarize {{ module }} briefly" in diff["unified"]
    assert client.get(f"{API}/prompts/{tid}/diff", params={"from": 1, "to": 9}).status_code == 404


def test_render_preview_keeps_secret_markers(client: TestClient) -> None:
    tid = create_prompt(client)["id"]
    res = client.post(f"{API}/prompts/{tid}/render", json={"inputs": {"words": 5}})
    body = res.json()
    assert body["valid"] is False and body["issues"][0]["variable"] == "words"
    ok = client.post(f"{API}/prompts/{tid}/render", json={"inputs": {"words": 50}}).json()
    assert ok["text"].endswith("Key: [secret:DOCS_TOKEN]") and ok["secret_refs"] == ["DOCS_TOKEN"]


def test_search_filters_archive_and_restore(client: TestClient) -> None:
    a = create_prompt(client)
    create_prompt(client, name="Other", tags=["ops"], owner="someone else")
    names = lambda **q: [t["name"] for t in client.get(f"{API}/prompts", params=q).json()["items"]]  # noqa: E731
    assert names(tag="docs") == ["Summarize a module"]
    assert names(tag=["docs", "ops"]) == []
    assert names(owner="platform") == ["Summarize a module"]
    assert names(q="other") == ["Other"]
    assert client.get(f"{API}/prompt-tags").json() == ["docs", "ops", "review"]
    res = client.patch(f"{API}/prompts/{a['id']}", json={"archived": True})
    assert res.json()["archived"] is True
    assert names() == ["Other"]
    assert names(archived="archived") == ["Summarize a module"]
    assert len(names(archived="all")) == 2
    assert (
        client.patch(f"{API}/prompts/{a['id']}", json={"archived": False}).json()["archived"]
        is False
    )
    assert client.patch(f"{API}/prompts/{a['id']}", json={"name": "Other"}).status_code == 409
    renamed = client.patch(f"{API}/prompts/{a['id']}", json={"name": "Renamed", "tags": ["x"]})
    assert renamed.json()["name"] == "Renamed" and renamed.json()["tags"] == ["x"]


def test_duplicate_export_import_round_trip(client: TestClient) -> None:
    p = create_prompt(client)
    client.post(
        f"{API}/prompts/{p['id']}/versions/save",
        json={"body": "v2 {{ module }}", "variables": [PROMPT["variables"][0]]},
    )
    dup = client.post(f"{API}/prompts/{p['id']}/duplicate", json={}).json()
    assert dup["name"] == "Summarize a module (copy)"
    assert dup["latest_version"] == 1 and dup["latest"]["body"] == "v2 {{ module }}"
    bundle = client.post(f"{API}/prompts/export", json={"template_ids": [p["id"]]}).json()
    assert bundle["format"] == "agenttape.prompts"
    assert [v["version"] for v in bundle["templates"][0]["versions"]] == [1, 2]
    res = client.post(f"{API}/prompts/import", json=bundle)
    assert res.status_code == 201
    created = res.json()["created"][0]
    assert created["name"] == "Summarize a module (imported)" and created["latest_version"] == 2
    skipped = client.post(f"{API}/prompts/import?on_conflict=skip", json=bundle).json()
    assert skipped["created"] == [] and skipped["skipped"][0]["name"] == "Summarize a module"
    bad = {
        **bundle,
        "templates": [
            {
                **bundle["templates"][0],
                "name": "Bad",
                "versions": [{"version": 1, "body": "{{ x }}", "variables": []}],
            }
        ],
    }
    res = client.post(f"{API}/prompts/import", json=bad)
    assert res.status_code == 422 and "undeclared" in str(res.json())
    all_names = [t["name"] for t in client.get(f"{API}/prompts").json()["items"]]
    assert "Bad" not in all_names


def test_used_versions_are_immutable(ctx: AppContext, client: TestClient) -> None:
    """The service enforces immutability: amending a version a run used is refused."""
    tid = simple_prompt(ctx)
    service = PromptLibraryService(ctx)
    v1 = PromptService(ctx).list_versions(tid)[0]
    amended = service.amend_version(
        tid,
        v1.id,
        PromptVersionAmend(
            body="Please repair {{ thing }}.", variables=[VariableDefinition(name="thing")]
        ),
    )
    assert amended.body == "Please repair {{ thing }}." and amended.version == 1
    pid = demo_profile(ctx)
    res = client.post(
        f"{API}/runs",
        json={"agent_profile_id": pid, "prompt_template_id": tid, "inputs": {"thing": "x"}},
    )
    assert res.status_code == 201, res.text
    with pytest.raises(ImmutableVersionError):
        service.amend_version(
            tid,
            v1.id,
            PromptVersionAmend(
                body="changed {{ thing }}", variables=[VariableDefinition(name="thing")]
            ),
        )
    res = client.put(
        f"{API}/prompts/{tid}/versions/{v1.id}",
        json={"body": "changed {{ thing }}", "variables": [{"name": "thing"}]},
    )
    assert res.status_code == 409 and res.json()["error"]["code"] == "version_immutable"
    new = service.save_version(
        tid,
        PromptVersionCreate(
            body="changed {{ thing }}", variables=[VariableDefinition(name="thing")]
        ),
    )
    assert new.version == 2 and new.locked_at is None
    assert PromptService(ctx).list_versions(tid)[1].body == "Please repair {{ thing }}."


def test_test_with_demo_agent_creates_a_real_run(ctx: AppContext, client: TestClient) -> None:
    tid = simple_prompt(ctx)
    res = client.post(f"{API}/prompts/{tid}/test-run", json={"inputs": {"thing": "greeter"}})
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["profile"]["adapter_type"] == "demo"
    run = body["run"]
    assert run["prompt_template_id"] == tid and "prompt-test" in run["tags"]
    detail = drive(ctx, run["id"])
    assert detail.run.status == "succeeded"
    assert detail.snapshot["prompt"]["rendered"] == "Please fix greeter."
    bad = client.post(f"{API}/prompts/{tid}/test-run", json={"inputs": {"nope": 1}})
    assert bad.status_code == 422


# --------------------------------------------------------------------------- profiles


def _script(tmp: Path) -> Path:
    path = tmp / "bin" / "tool"
    path.parent.mkdir(exist_ok=True)
    path.write_text(f"#!{sys.executable}\nprint('ok')\n")
    path.chmod(0o755)
    return path


def test_profile_validation_allowlist_versions_and_archive(
    ctx: AppContext, client: TestClient
) -> None:
    exe = _script(ctx.settings.data_dir.parent)
    cfg = {"executable": str(exe), "args": ["--x"], "timeout_seconds": 30}
    res = client.post(
        f"{API}/agent-profiles/validate", json={"adapter_type": "generic-command", "config": cfg}
    )
    assert res.json()["valid"] is False
    check = client.post(
        f"{API}/settings/allowed-executables/check", json={"executable": str(exe)}
    ).json()
    assert check["allowed"] is False and check["path"] == str(exe)
    allow = client.put(f"{API}/settings/allowed-executables", json={"executables": [str(exe)]})
    assert allow.status_code == 200 and allow.json()["stored"] == [str(exe)]
    assert (
        client.put(
            f"{API}/settings/allowed-executables", json={"executables": ["/no/such/tool"]}
        ).status_code
        == 422
    )
    res = client.post(
        f"{API}/agent-profiles/validate", json={"adapter_type": "generic-command", "config": cfg}
    )
    assert res.json() == {"valid": True, "issues": []}
    bad = client.post(
        f"{API}/agent-profiles/validate",
        json={
            "adapter_type": "generic-command",
            "config": {**cfg, "timeout_seconds": -1, "budget": {"max_cost_usd": 0}, "args": "x y"},
        },
    ).json()
    assert {i["field"] for i in bad["issues"]} >= {"timeout_seconds", "budget.max_cost_usd", "args"}
    created = client.post(
        f"{API}/agent-profiles",
        json={"name": "Script", "adapter_type": "generic-command", "config": cfg},
    )
    assert created.status_code == 201
    pid = created.json()["id"]
    v2 = client.post(
        f"{API}/agent-profiles/{pid}/versions", json={"config": {**cfg, "args": ["--y"]}}
    )
    assert v2.status_code == 201 and v2.json()["version"] == 2
    rejected = client.post(
        f"{API}/agent-profiles/{pid}/versions", json={"config": {**cfg, "executable": "/bin/nope"}}
    )
    assert rejected.status_code == 422
    assert [v["version"] for v in client.get(f"{API}/agent-profiles/{pid}/versions").json()] == [
        2,
        1,
    ]
    archived = client.patch(f"{API}/agent-profiles/{pid}", json={"archived": True}).json()
    assert archived["archived"] is True
    listed = client.get(f"{API}/agent-profiles").json()["items"]
    assert pid not in [p["id"] for p in listed]
    run = client.post(f"{API}/runs", json={"agent_profile_id": pid, "prompt": "hi"})
    assert run.status_code == 409


def test_secret_references_never_expose_values(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = "ghp_" + "A" * 36
    monkeypatch.setenv("GITHUB_TOKEN_FOR_TESTS", value)
    res = client.post(
        f"{API}/secret-references",
        json={"name": "GITHUB_TOKEN", "locator": "GITHUB_TOKEN_FOR_TESTS", "description": "CI"},
    )
    assert res.status_code == 201
    body = res.json()
    assert body["available"] is True and value not in res.text
    assert client.post(f"{API}/secret-references", json={"name": "GITHUB_TOKEN"}).status_code == 409
    assert client.post(f"{API}/secret-references", json={"name": "bad name"}).status_code == 422
    listed = client.get(f"{API}/secret-references")
    assert value not in listed.text and listed.json()[0]["locator"] == "GITHUB_TOKEN_FOR_TESTS"
    upd = client.patch(f"{API}/secret-references/{body['id']}", json={"locator": "MISSING_VAR"})
    assert upd.json()["available"] is False
    assert client.delete(f"{API}/secret-references/{body['id']}").status_code == 204
    assert client.get(f"{API}/secret-references").json() == []


# --------------------------------------------------------------------------- projects


def test_project_path_checks_and_symlink_escape(ctx: AppContext, client: TestClient) -> None:
    root = ctx.settings.allowed_roots[0]
    repo = root / "app"
    repo.mkdir()
    ok = client.post(f"{API}/projects/check-path", json={"path": str(repo)}).json()
    assert ok["allowed"] is True and ok["canonical"] == str(repo.resolve())
    assert ok["is_git_repo"] is False and "Not a Git repository" in ok["message"]
    outside = ctx.settings.data_dir.parent / "elsewhere"
    outside.mkdir()
    res = client.post(f"{API}/projects/check-path", json={"path": str(outside)}).json()
    assert res["allowed"] is False and "Outside" in res["message"]
    link = root / "sneaky"
    link.symlink_to(outside)
    res = client.post(f"{API}/projects/check-path", json={"path": str(link)}).json()
    assert res["allowed"] is False and res["canonical"] == str(outside.resolve())
    rel = client.post(f"{API}/projects/check-path", json={"path": "relative/dir"}).json()
    assert rel["allowed"] is False and "absolute" in rel["message"]
    assert (
        client.post(f"{API}/projects", json={"name": "Sneaky", "repo_path": str(link)}).status_code
        == 403
    )


def test_project_crud_activity_and_archive(ctx: AppContext, client: TestClient) -> None:
    repo = ctx.settings.allowed_roots[0] / "svc"
    repo.mkdir()
    created = client.post(
        f"{API}/projects",
        json={
            "name": "Service",
            "repo_path": str(repo),
            "settings": {
                "timeout_seconds": 60,
                "validation_commands": [["pytest", "-q"]],
                "protected_paths": [".github/**"],
                "env_allowlist": ["CI"],
                "git_isolation": True,
            },
        },
    )
    assert created.status_code == 201, created.text
    pid = created.json()["id"]
    pid_profile = demo_profile(ctx)
    run = client.post(
        f"{API}/runs", json={"agent_profile_id": pid_profile, "prompt": "go", "project_id": pid}
    ).json()["run"]
    drive(ctx, run["id"])
    activity = client.get(f"{API}/projects/{pid}/activity").json()
    assert activity["runs_total"] == 1 and activity["runs"][0]["id"] == run["id"]
    assert sum(activity["status_counts"].values()) == 1
    assert activity["schedules"]["available"] is True and activity["schedules"]["total"] == 0
    assert activity["workflows"]["available"] is True
    upd = client.patch(
        f"{API}/projects/{pid}", json={"settings": {"timeout_seconds": 90, "git_isolation": False}}
    ).json()
    assert upd["settings"]["timeout_seconds"] == 90
    assert client.patch(f"{API}/projects/{pid}", json={"archived": True}).json()["archived"]
    res = client.post(
        f"{API}/runs", json={"agent_profile_id": pid_profile, "prompt": "go", "project_id": pid}
    )
    assert res.status_code == 409


# --------------------------------------------------------------------------- onboarding


def test_onboarding_state(ctx: AppContext, client: TestClient) -> None:
    state = client.get(f"{API}/onboarding").json()
    assert state["show"] is True and state["has_user_project"] is False
    state = client.post(f"{API}/onboarding", json={"action": "dismiss"}).json()
    assert state["show"] is False and state["dismissed"] is True
    state = client.post(f"{API}/onboarding", json={"action": "reset"}).json()
    assert state["show"] is True
    repo = ctx.settings.allowed_roots[0] / "mine"
    repo.mkdir()
    client.post(f"{API}/projects", json={"name": "Mine", "repo_path": str(repo)})
    assert client.get(f"{API}/onboarding").json()["show"] is True  # re-opened explicitly
    state = client.post(f"{API}/onboarding", json={"action": "complete"}).json()
    assert state["show"] is False and state["completed"] is True
    assert client.post(f"{API}/onboarding", json={"action": "bogus"}).status_code == 422
