from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agenttape.api.app import create_app
from agenttape.application.context import AppContext
from agenttape.application.services.demo import DemoService
from agenttape.bootstrap import build_context
from agenttape.security.tokens import hash_token
from tests.conftest import demo_profile, drive, make_settings, start_run


def test_health_and_ready(client: TestClient) -> None:
    assert client.get("/api/v1/health").json()["status"] == "ok"
    ready = client.get("/api/v1/ready").json()
    assert ready["status"] == "ready"
    assert {c["name"] for c in ready["checks"]} == {"database", "migrations", "data_dir"}


def test_openapi_docs(client: TestClient) -> None:
    schema = client.get("/api/openapi.json").json()
    assert (
        "/api/v1/runs" in schema["paths"]
        and "/api/v1/runs/{run_id}/events/stream" in schema["paths"]
    )
    assert client.get("/api/docs").status_code == 200


def test_error_envelope(client: TestClient) -> None:
    missing = client.get("/api/v1/runs/does-not-exist")
    assert missing.status_code == 404
    assert missing.json() == {
        "error": {
            "code": "not_found",
            "message": "run 'does-not-exist' was not found",
            "details": {"resource": "run", "id": "does-not-exist"},
        }
    }
    invalid = client.post("/api/v1/runs", json={"priority": "high"})
    assert invalid.status_code == 422 and invalid.json()["error"]["code"] == "validation_error"
    assert invalid.json()["error"]["details"]["errors"]
    unknown = client.get("/api/v1/nope")
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "not_found"


def test_create_run_idempotent_with_header(client: TestClient, ctx: AppContext) -> None:
    pid = demo_profile(ctx)
    body = {"agent_profile_id": pid, "prompt": "hi", "source": "api"}
    first = client.post("/api/v1/runs", json=body, headers={"Idempotency-Key": "abc"})
    again = client.post("/api/v1/runs", json=body, headers={"Idempotency-Key": "abc"})
    assert first.status_code == 201 and first.json()["created"] is True
    assert again.status_code == 200 and again.json()["created"] is False
    assert first.json()["run"]["id"] == again.json()["run"]["id"]
    changed = client.post(
        "/api/v1/runs", json={**body, "prompt": "different"}, headers={"Idempotency-Key": "abc"}
    )
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "idempotency_key_conflict"
    assert changed.json()["error"]["details"]["existing_run_id"] == first.json()["run"]["id"]


def test_run_lifecycle_over_http(client: TestClient, ctx: AppContext) -> None:
    pid = demo_profile(ctx)
    run_id = client.post("/api/v1/runs", json={"agent_profile_id": pid, "prompt": "go"}).json()[
        "run"
    ]["id"]
    drive(ctx, run_id)
    detail = client.get(f"/api/v1/runs/{run_id}").json()
    assert detail["run"]["status"] == "succeeded" and detail["event_count"] > 10
    page = client.get(f"/api/v1/runs/{run_id}/events", params={"limit": 5}).json()
    assert [e["sequence"] for e in page["items"]] == [1, 2, 3, 4, 5]
    assert page["next_after_sequence"] == 5
    nxt = client.get(
        f"/api/v1/runs/{run_id}/events", params={"after_sequence": 5, "limit": 5}
    ).json()
    assert nxt["items"][0]["sequence"] == 6
    only_tools = client.get(f"/api/v1/runs/{run_id}/events", params=[("type", "tool_call")]).json()
    assert {e["type"] for e in only_tools["items"]} == {"tool_call"}
    text = client.get(f"/api/v1/runs/{run_id}/events", params={"q": "greeter"}).json()
    assert all("greeter" in e["summary"].lower() for e in text["items"]) and text["items"]
    listing = client.get("/api/v1/runs", params=[("status", "succeeded"), ("limit", "1")]).json()
    assert listing["total"] == 1 and listing["items"][0]["id"] == run_id
    log = client.get(f"/api/v1/runs/{run_id}/logs/1/stdout")
    assert log.status_code == 200 and "syntax ok" in log.text
    assert client.get(f"/api/v1/runs/{run_id}/logs/1/other").status_code == 422
    export = client.get(f"/api/v1/runs/{run_id}/export")
    bundle = json.loads(export.text)
    assert (
        bundle["format"] == "agenttape.run-bundle"
        and len(bundle["events"]) == detail["event_count"]
    )
    assert any(
        a["name"] == "summary.md" and "Demo Agent report" in a["content"]
        for a in bundle["artifacts"]
    )


def test_cancel_and_retry_endpoints(client: TestClient, ctx: AppContext) -> None:
    run_id = start_run(ctx, demo_profile(ctx))
    cancelled = client.post(f"/api/v1/runs/{run_id}/cancel", json={"reason": "stop"})
    assert cancelled.json()["status"] == "cancelled"
    retried = client.post(f"/api/v1/runs/{run_id}/retry")
    assert retried.json()["status"] == "queued"
    drive(ctx, run_id)
    conflict = client.post(f"/api/v1/runs/{run_id}/cancel")
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "conflict"


def test_artifacts_are_served_safely(client: TestClient, ctx: AppContext) -> None:
    run_id = start_run(ctx, demo_profile(ctx))
    drive(ctx, run_id)
    arts = client.get(f"/api/v1/runs/{run_id}/artifacts").json()
    summary = next(a for a in arts if a["name"] == "summary.md")
    content = client.get(f"/api/v1/artifacts/{summary['id']}/content")
    assert content.status_code == 200 and "Demo Agent report" in content.text
    assert content.headers["x-content-type-options"] == "nosniff"
    assert len(content.content) == summary["size_bytes"]
    import hashlib

    assert hashlib.sha256(content.content).hexdigest() == summary["sha256"]
    assert client.get("/api/v1/artifacts/nope/content").status_code == 404


def test_artifact_store_rejects_traversal(ctx: AppContext) -> None:
    from agenttape.domain.errors import PolicyViolationError

    for bad in ("../../etc/passwd", "/etc/passwd", "runs/../../x"):
        with pytest.raises(PolicyViolationError):
            ctx.artifact_store.resolve(bad)


def test_artifact_size_cap_and_atomicity(ctx: AppContext) -> None:
    from agenttape.artifacts.store import ArtifactTooLargeError
    from agenttape.infrastructure.artifact_store import FilesystemArtifactStore

    store = FilesystemArtifactStore(ctx.settings.artifacts_dir, max_bytes=10)
    with pytest.raises(ArtifactTooLargeError):
        store.put_bytes("runs/x/big.txt", b"x" * 11)
    assert not (ctx.settings.artifacts_dir / "runs" / "x" / "big.txt").exists()
    assert not list((ctx.settings.artifacts_dir / "runs" / "x").glob(".tmp-*"))
    obj = store.put_bytes("runs/x/ok.txt", b"hello")
    assert obj.size_bytes == 5 and len(obj.sha256) == 64


def test_prompt_profile_project_endpoints(
    client: TestClient, ctx: AppContext, tmp_path: Path
) -> None:
    created = client.post(
        "/api/v1/prompts",
        json={
            "name": "P",
            "body": "Hi {{ who }} {{ secret:TOKEN }}",
            "variables": [{"name": "who"}],
        },
    )
    assert created.status_code == 201
    tid = created.json()["id"]
    preview = client.post(f"/api/v1/prompts/{tid}/render", json={"inputs": {"who": "Ada"}}).json()
    assert preview == {
        "text": "Hi Ada [secret:TOKEN]",
        "secret_refs": ["TOKEN"],
        "issues": [],
        "valid": True,
    }
    bad = client.post("/api/v1/prompts", json={"name": "Q", "body": "Hi {{ nobody }}"})
    assert bad.status_code == 422 and bad.json()["error"]["details"]["undeclared"] == ["nobody"]
    assert (
        client.post(
            f"/api/v1/prompts/{tid}/versions",
            json={"body": "v2 {{ who }}", "variables": [{"name": "who"}]},
        ).json()["version"]
        == 2
    )
    adapters = client.get("/api/v1/adapters").json()
    assert any(a["name"] == "demo" and a["status"] == "ready" for a in adapters)
    check = client.post(
        "/api/v1/agent-profiles/validate",
        json={"adapter_type": "demo", "config": {"scenario": "zzz"}},
    ).json()
    assert check["valid"] is False
    repo = tmp_path / "repos" / "api-repo"
    repo.mkdir(parents=True)
    proj = client.post("/api/v1/projects", json={"name": "Repo", "repo_path": str(repo)})
    assert proj.status_code == 201
    denied = client.post("/api/v1/projects", json={"name": "Etc", "repo_path": "/etc"})
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "policy_violation"
    roots = client.get("/api/v1/settings/allowed-roots").json()
    assert str((tmp_path / "repos").resolve()) in roots["effective"]
    assert client.put("/api/v1/settings/allowed-roots", json={"roots": ["/"]}).status_code == 422


def test_demo_endpoints_and_approvals(client: TestClient, ctx: AppContext) -> None:
    scenarios = client.get("/api/v1/demo/scenarios").json()
    assert {s["scenario"] for s in scenarios} == {
        "success",
        "fail_then_retry",
        "needs_approval",
        "timeout",
        "fail",
    }
    started = client.post("/api/v1/demo/runs", json={"scenario": "needs_approval"})
    assert started.status_code == 201
    run_id = started.json()["run"]["id"]
    drive(ctx, run_id)
    pending = client.get("/api/v1/approvals", params={"status": "pending"}).json()
    assert pending["total"] == 1
    approval_id = pending["items"][0]["id"]
    decided = client.post(f"/api/v1/approvals/{approval_id}/approve", json={"note": "ok"})
    assert decided.json()["status"] == "approved"
    assert drive(ctx, run_id).run.status == "succeeded"
    audit = client.get("/api/v1/audit", params={"resource_id": approval_id}).json()
    assert {e["action"] for e in audit["items"]} >= {"approval.request", "approval.approved"}
    assert client.post("/api/v1/demo/reset", json={"confirm": "nope"}).status_code == 422
    reset = client.post("/api/v1/demo/reset", json={"confirm": "reset-demo"}).json()
    assert reset["deleted"]["runs"] >= 1  # seeds may add demo runs (the Git compare pair)


def test_spa_fallback_serves_index(ctx: AppContext, tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>app</html>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    with TestClient(create_app(ctx, web_dist=dist), base_url="http://127.0.0.1") as c:
        assert c.get("/runs/abc").text == "<html>app</html>"
        assert c.get("/assets/app.js").text == "console.log(1)"
        assert "immutable" in c.get("/assets/app.js").headers["cache-control"]
        assert c.get("/..%2F..%2Fetc%2Fpasswd").text == "<html>app</html>"
        assert c.get("/api/v1/unknown").status_code == 404


def test_body_size_limit(tmp_path: Path) -> None:
    ctx = build_context(make_settings(tmp_path, max_request_body_bytes=100), migrate=True)
    try:
        with TestClient(create_app(ctx), base_url="http://127.0.0.1") as c:
            r = c.post(
                "/api/v1/prompts", content=b"x" * 500, headers={"content-type": "application/json"}
            )
            assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"
    finally:
        ctx.close()


class TestAuthBeyondLoopback:
    def _client(self, tmp_path: Path, **overrides: object) -> tuple[TestClient, AppContext]:
        ctx = build_context(make_settings(tmp_path, host="0.0.0.0", **overrides), migrate=True)
        return TestClient(create_app(ctx), base_url="http://127.0.0.1"), ctx

    def test_token_required(self, tmp_path: Path) -> None:
        token = "atk_test-token-value-0123456789"
        client, ctx = self._client(tmp_path, api_token_hash=hash_token(token))
        with client:
            assert client.get("/api/v1/health").status_code == 200
            assert client.get("/api/v1/ready").status_code == 200
            denied = client.get("/api/v1/runs")
            assert denied.status_code == 401 and denied.json()["error"]["code"] == "unauthorized"
            assert (
                client.get("/api/v1/runs", headers={"Authorization": "Bearer wrong"}).status_code
                == 401
            )
            assert (
                client.get("/api/v1/runs", headers={"Authorization": f"Bearer {token}"}).status_code
                == 200
            )
            assert (
                client.get("/api/v1/runs", headers={"X-AgentTape-Token": token}).status_code == 200
            )
        ctx.close()

    def test_missing_token_configuration_blocks_api(self, tmp_path: Path) -> None:
        client, ctx = self._client(tmp_path)
        with client:
            assert client.get("/api/v1/runs").status_code == 503
            assert client.get("/api/v1/health").status_code == 200
        ctx.close()

    def test_failed_auth_is_rate_limited(self, tmp_path: Path) -> None:
        client, ctx = self._client(tmp_path, api_token_hash=hash_token("atk_real"))
        with client:
            codes = [
                client.get("/api/v1/runs", headers={"Authorization": "Bearer bad"}).status_code
                for _ in range(25)
            ]
            assert codes[0] == 401 and codes[-1] == 429
        ctx.close()

    def test_optional_token_on_loopback(self, tmp_path: Path) -> None:
        ctx = build_context(
            make_settings(tmp_path, require_token=True, api_token_hash=hash_token("atk_x")),
            migrate=True,
        )
        with TestClient(create_app(ctx), base_url="http://127.0.0.1") as c:
            assert c.get("/api/v1/runs").status_code == 401
            assert (
                c.get("/api/v1/runs", headers={"Authorization": "Bearer atk_x"}).status_code == 200
            )
        ctx.close()


def test_demo_boundary_reset_only_touches_demo_rows(
    client: TestClient, ctx: AppContext, tmp_path: Path
) -> None:
    demo = DemoService(ctx)
    report = demo.seed()
    assert report.created["agent_profile"] and report.created["prompt"]
    again = demo.seed()
    assert not again.created  # idempotent
    demo_run = demo.start_run(scenario="success").run.id
    drive(ctx, demo_run)
    mine_profile = demo_profile(ctx, name="my own profile")
    mine_run = start_run(ctx, mine_profile)
    drive(ctx, mine_run)
    repo = tmp_path / "repos" / "mine"
    repo.mkdir(parents=True)
    client.post("/api/v1/projects", json={"name": "My project", "repo_path": str(repo)})
    client.post("/api/v1/prompts", json={"name": "My prompt", "body": "hello"})
    artifacts_dir = ctx.settings.artifacts_dir / "runs"
    assert (artifacts_dir / demo_run).is_dir() and (artifacts_dir / mine_run).is_dir()
    deleted = demo.reset()
    # Seeds may add demo runs and projects (the isolated project and compare pair).
    assert deleted["runs"] >= 1 and deleted["agent_profiles"] >= 5 and deleted["projects"] >= 1
    runs = client.get("/api/v1/runs").json()
    assert [r["id"] for r in runs["items"]] == [mine_run]
    assert [p["name"] for p in client.get("/api/v1/projects").json()["items"]] == ["My project"]
    assert [p["name"] for p in client.get("/api/v1/prompts").json()["items"]] == ["My prompt"]
    assert [p["name"] for p in client.get("/api/v1/agent-profiles").json()["items"]] == [
        "my own profile"
    ]
    assert not (artifacts_dir / demo_run).exists()
    assert (artifacts_dir / mine_run).is_dir()  # non-demo artifacts are untouched
    assert demo.reset() == {}


class TestBrowserAttacksOnLoopback:
    """With no token (the loopback default), a web page must not be able to drive the API:
    not by DNS rebinding (a foreign Host) and not by cross-site writes (a foreign Origin)."""

    def test_dns_rebinding_host_is_refused(self, client: TestClient) -> None:
        for host in ("attacker.example", "attacker.example:8787", "192.168.1.20"):
            r = client.get("/api/v1/runs", headers={"Host": host})
            assert r.status_code == 421 and r.json()["error"]["code"] == "untrusted_host"
            put = client.put(
                "/api/v1/settings/allowed-executables",
                json={"executables": ["/bin/echo"]},
                headers={"Host": host},
            )
            assert put.status_code == 421
        for host in ("127.0.0.1:8787", "localhost:8787", "[::1]:8787", "localhost"):
            assert client.get("/api/v1/runs", headers={"Host": host}).status_code == 200

    def test_cross_site_writes_are_refused(self, client: TestClient) -> None:
        for headers in (
            {"Origin": "https://attacker.example"},
            {"Origin": "null"},
            {"Sec-Fetch-Site": "cross-site"},
        ):
            r = client.post("/api/v1/demo/seed", headers=headers)
            assert r.status_code == 403 and r.json()["error"]["code"] == "cross_site_request"
        # Reads stay readable only by the browser's same-origin rules; the UI's own writes pass.
        assert (
            client.get("/api/v1/runs", headers={"Origin": "https://x.example"}).status_code == 200
        )
        ok = client.post(
            "/api/v1/onboarding",
            json={"action": "dismiss"},
            headers={"Origin": "http://127.0.0.1:8787", "Sec-Fetch-Site": "same-origin"},
        )
        assert ok.status_code == 200

    def test_configured_host_is_accepted(self, tmp_path: Path) -> None:
        ctx = build_context(make_settings(tmp_path, allowed_hosts=["agenttape.lan"]), migrate=True)
        with TestClient(create_app(ctx), base_url="http://agenttape.lan") as c:
            assert c.get("/api/v1/runs").status_code == 200
        ctx.close()

    def test_webhooks_skip_browser_checks(self, client: TestClient) -> None:
        # Webhooks authenticate with their own token and may arrive through a tunnel.
        r = client.post("/api/v1/webhooks/nope", json={}, headers={"Host": "hooks.example"})
        assert r.status_code != 421

    def test_public_prefixes_match_whole_segments(self, client: TestClient) -> None:
        from agenttape.api.app import GuardMiddleware

        guard = GuardMiddleware(
            client.app, ctx=None, public_prefixes=["/api/v1/health", "/api/v1/webhooks/"]
        )  # type: ignore[arg-type]
        assert guard._is_public("/api/v1/health")
        assert guard._is_public("/api/v1/webhooks/abc")
        assert not guard._is_public("/api/v1/health-admin")
        assert not guard._is_public("/api/v1/webhooks-admin")

    def test_security_headers(self, ctx: AppContext, tmp_path: Path) -> None:
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "index.html").write_text("<html><script>var a=1;</script>app</html>")
        with TestClient(create_app(ctx, web_dist=dist), base_url="http://127.0.0.1") as c:
            page = c.get("/runs")
            csp = page.headers["content-security-policy"]
            assert "frame-ancestors 'none'" in csp and "'sha256-" in csp
            assert page.headers["x-frame-options"] == "DENY"
            api = c.get("/api/v1/runs")
            assert api.headers["x-content-type-options"] == "nosniff"


def test_data_dir_is_owner_only(tmp_path: Path) -> None:
    import stat

    settings = make_settings(tmp_path)
    settings.ensure_dirs()
    assert stat.S_IMODE(settings.data_dir.stat().st_mode) == 0o700
