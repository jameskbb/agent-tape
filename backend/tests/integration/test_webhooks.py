"""Webhook triggers over HTTP: auth, idempotency, size/content-type/JSON rejection,
input mapping, rate limiting, and the delivery log."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agenttape.api.app import create_app
from agenttape.application.context import AppContext
from agenttape.application.schemas.catalog import PromptTemplateCreate
from agenttape.application.services.prompts import PromptService
from agenttape.bootstrap import build_context
from agenttape.domain.enums import VariableType
from agenttape.domain.prompts import VariableDefinition
from agenttape.infrastructure.db.models import Run, Trigger
from agenttape.security.tokens import hash_token
from tests.conftest import demo_profile, make_settings

JSON = {"Content-Type": "application/json"}


def branch_prompt(ctx: AppContext) -> str:
    return (
        PromptService(ctx)
        .create_template(
            PromptTemplateCreate(
                name="Check a branch",
                body="Check {{ branch }} for {{ reason }}. Token {{ ci_token }}.",
                variables=[
                    VariableDefinition(name="branch"),
                    VariableDefinition(name="reason", default="a push"),
                    VariableDefinition(
                        name="ci_token", type=VariableType.SECRET, default="CI_TOKEN"
                    ),
                ],
            )
        )
        .id
    )


def create_trigger(client: TestClient, ctx: AppContext, **overrides: Any) -> tuple[str, str]:
    body: dict[str, Any] = {
        "name": "GitHub dispatch",
        "prompt_template_id": branch_prompt(ctx),
        "agent_profile_id": demo_profile(ctx),
        "input_mapping": {"branch": "$.client_payload.branch"},
    }
    body.update(overrides)
    resp = client.post("/api/v1/triggers", json=body)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    return data["trigger"]["id"], data["token"]


def post(
    client: TestClient, trigger_id: str, token: str | None, payload: Any, **headers: str
) -> Any:
    h = dict(JSON)
    if token:
        h["Authorization"] = f"Bearer {token}"
    h.update(headers)
    return client.post(f"/api/v1/webhooks/{trigger_id}", content=json.dumps(payload), headers=h)


def test_token_is_shown_once_and_hashed_at_rest(client: TestClient, ctx: AppContext) -> None:
    tid, token = create_trigger(client, ctx)
    assert token.startswith("atw_") and len(token) > 30
    detail = client.get(f"/api/v1/triggers/{tid}").json()
    assert "token" not in detail and token not in json.dumps(detail)
    assert detail["token_prefix"] == token[:12]
    with ctx.session() as session:
        t = session.get(Trigger, tid)
        assert t is not None and t.token_hash == hash_token(token)
    rotated = client.post(f"/api/v1/triggers/{tid}/rotate-token").json()["token"]
    assert rotated != token
    assert post(client, tid, token, {"client_payload": {"branch": "x"}}).status_code == 401
    assert post(client, tid, rotated, {"client_payload": {"branch": "x"}}).status_code == 202


def test_accepted_delivery_creates_a_webhook_run_with_mapped_inputs(
    client: TestClient, ctx: AppContext
) -> None:
    tid, token = create_trigger(client, ctx)
    resp = post(
        client,
        tid,
        token,
        {"event_type": "agenttape", "client_payload": {"branch": "feature/x"}},
        **{"X-GitHub-Delivery": "d-1", "X-GitHub-Event": "repository_dispatch"},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "accepted" and body["idempotency_source"] == "header:x-github-delivery"
    with ctx.session() as session:
        run = session.get(Run, body["run_id"])
        assert run is not None
        assert run.source == "webhook" and run.trigger_id == tid
        assert run.inputs["branch"] == "feature/x"
        assert run.inputs["ci_token"] == "CI_TOKEN"  # default secret *name*, never a value
    events = client.get(f"/api/v1/runs/{body['run_id']}/events").json()["items"]
    assert any(e["type"] == "trigger" for e in events)
    log = client.get(f"/api/v1/triggers/{tid}/deliveries").json()
    assert log["total"] == 1
    entry = log["items"][0]
    assert entry["status"] == "accepted" and entry["run_id"] == body["run_id"]
    assert entry["metadata"]["headers"]["x-github-event"] == "repository_dispatch"


@pytest.mark.parametrize(
    ("headers", "source"),
    [
        ({"Idempotency-Key": "abc-123"}, "header:idempotency-key"),
        ({"X-GitHub-Delivery": "72d3162e"}, "header:x-github-delivery"),
        ({}, "body-sha256"),
    ],
)
def test_duplicate_deliveries_return_the_same_run(
    client: TestClient, ctx: AppContext, headers: dict[str, str], source: str
) -> None:
    tid, token = create_trigger(client, ctx)
    payload = {"client_payload": {"branch": "main"}}
    first = post(client, tid, token, payload, **headers)
    second = post(client, tid, token, payload, **headers)
    assert first.status_code == 202 and second.status_code == 200
    assert second.json()["duplicate"] is True
    assert first.json()["run_id"] == second.json()["run_id"]
    assert first.json()["idempotency_source"] == source
    with ctx.session() as session:
        assert len(session.scalars(select(Run).where(Run.trigger_id == tid)).all()) == 1
    statuses = [
        d["status"] for d in client.get(f"/api/v1/triggers/{tid}/deliveries").json()["items"]
    ]
    assert sorted(statuses) == ["accepted", "duplicate"]


def test_different_delivery_ids_create_separate_runs(client: TestClient, ctx: AppContext) -> None:
    tid, token = create_trigger(client, ctx)
    payload = {"client_payload": {"branch": "main"}}
    a = post(client, tid, token, payload, **{"X-GitHub-Delivery": "1"})
    b = post(client, tid, token, payload, **{"X-GitHub-Delivery": "2"})
    assert a.status_code == b.status_code == 202
    assert a.json()["run_id"] != b.json()["run_id"]


def test_idempotency_from_payload_path(client: TestClient, ctx: AppContext) -> None:
    tid, token = create_trigger(client, ctx, idempotency_key_path="$.delivery.id")
    a = post(client, tid, token, {"delivery": {"id": 7}, "client_payload": {"branch": "a"}})
    b = post(client, tid, token, {"delivery": {"id": 7}, "client_payload": {"branch": "b"}})
    assert a.status_code == 202 and b.status_code == 200
    assert a.json()["idempotency_source"] == "payload:$.delivery.id"


def test_require_mode_rejects_deliveries_without_a_key(client: TestClient, ctx: AppContext) -> None:
    tid, token = create_trigger(client, ctx, idempotency="require")
    resp = post(client, tid, token, {"client_payload": {"branch": "a"}})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "idempotency_key_required"
    ok = post(client, tid, token, {"client_payload": {"branch": "a"}}, **{"Idempotency-Key": "k"})
    assert ok.status_code == 202


def test_authentication_variants_and_failures(client: TestClient, ctx: AppContext) -> None:
    tid, token = create_trigger(client, ctx)
    payload = {"client_payload": {"branch": "main"}}
    assert post(client, tid, None, payload).status_code == 401
    assert post(client, tid, "atw_wrong", payload).status_code == 401
    via_header = client.post(
        f"/api/v1/webhooks/{tid}",
        content=json.dumps(payload),
        headers={**JSON, "X-AgentTape-Token": token, "Idempotency-Key": "h"},
    )
    assert via_header.status_code == 202
    # Query-string tokens are refused unless the trigger opts in.
    via_query = client.post(
        f"/api/v1/webhooks/{tid}?token={token}",
        content=json.dumps(payload),
        headers={**JSON, "Idempotency-Key": "q"},
    )
    assert via_query.status_code == 401
    assert via_query.json()["error"]["code"] == "query_token_disabled"
    wrong_query = client.post(
        f"/api/v1/webhooks/{tid}?token=atw_wrong", content=json.dumps(payload), headers=JSON
    )
    assert wrong_query.json()["error"]["code"] == "query_token_disabled"  # no oracle
    opted = client.patch(f"/api/v1/triggers/{tid}", json={"allow_query_token": True})
    assert opted.status_code == 200 and opted.json()["config"]["allow_query_token"] is True
    via_query = client.post(
        f"/api/v1/webhooks/{tid}?token={token}",
        content=json.dumps(payload),
        headers={**JSON, "Idempotency-Key": "q"},
    )
    assert via_query.status_code == 202
    assert (
        client.get(f"/api/v1/triggers/{tid}/deliveries").json()["items"][0]["metadata"]["auth"]
        == "query"
    )
    assert post(client, "no-such-trigger", token, payload).status_code == 404
    # Unauthenticated attempts are not written to the delivery log.
    statuses = [
        d["status"] for d in client.get(f"/api/v1/triggers/{tid}/deliveries").json()["items"]
    ]
    assert statuses.count("accepted") == 2 and len(statuses) == 2


def test_failed_auth_is_rate_limited(client: TestClient, ctx: AppContext) -> None:
    tid, _token = create_trigger(client, ctx)
    codes = [post(client, tid, "atw_bad", {}).status_code for _ in range(25)]
    assert codes[:20] == [401] * 20
    assert 429 in codes[20:]


def test_size_content_type_and_json_rejections_are_logged(
    client: TestClient, ctx: AppContext
) -> None:
    tid, token = create_trigger(client, ctx, max_payload_bytes=512)
    auth = {"Authorization": f"Bearer {token}"}
    big = post(client, tid, token, {"client_payload": {"branch": "x" * 1000}})
    assert big.status_code == 413
    text = client.post(
        f"/api/v1/webhooks/{tid}",
        content=b"branch=main",
        headers={**auth, "Content-Type": "text/plain"},
    )
    assert text.status_code == 415
    form = client.post(
        f"/api/v1/webhooks/{tid}",
        content=b"a=1",
        headers={**auth, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert form.status_code == 415
    bad = client.post(f"/api/v1/webhooks/{tid}", content=b"{not json", headers={**auth, **JSON})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_json"
    empty = client.post(f"/api/v1/webhooks/{tid}", content=b"", headers={**auth, **JSON})
    assert empty.status_code == 400
    log = client.get(f"/api/v1/triggers/{tid}/deliveries").json()["items"]
    assert sorted(d["http_status"] for d in log) == [400, 400, 413, 415, 415]
    assert all(d["status"] == "rejected" and d["run_id"] is None for d in log)


def test_hard_body_cap_applies_before_lookup(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/webhooks/whatever",
        content=b"{" + b" " * (4 * 1024 * 1024 + 10) + b"}",
        headers=JSON,
    )
    assert resp.status_code == 413


def test_mapping_failure_and_missing_required_input(client: TestClient, ctx: AppContext) -> None:
    tid, token = create_trigger(client, ctx)
    resp = post(client, tid, token, {"client_payload": {}})
    assert resp.status_code == 422
    assert "branch" in resp.json()["error"]["message"]
    sample = client.post(
        f"/api/v1/triggers/{tid}/test-mapping",
        json={"payload": {"client_payload": {"branch": "dev"}}},
    ).json()
    assert sample == {"inputs": {"branch": "dev"}, "missing": [], "errors": []}


def test_trigger_validation(client: TestClient, ctx: AppContext) -> None:
    prompt = branch_prompt(ctx)
    profile = demo_profile(ctx)
    base = {"name": "t", "prompt_template_id": prompt, "agent_profile_id": profile}
    bad_path = client.post("/api/v1/triggers", json={**base, "input_mapping": {"branch": "$..x"}})
    assert bad_path.status_code == 422
    unknown = client.post("/api/v1/triggers", json={**base, "input_mapping": {"nope": "$.a"}})
    assert unknown.status_code == 422
    secret = client.post(
        "/api/v1/triggers", json={**base, "input_mapping": {"ci_token": "$.token"}}
    )
    assert secret.status_code == 422 and "secret" in secret.json()["error"]["message"]
    short = client.post("/api/v1/triggers", json={**base, "token": "short"})
    assert short.status_code == 422
    own = client.post(
        "/api/v1/triggers", json={**base, "token": "my-own-long-webhook-token-123456"}
    )
    assert own.status_code == 201 and own.json()["token"] == "my-own-long-webhook-token-123456"


def test_disabled_trigger_and_per_trigger_rate_limit(client: TestClient, ctx: AppContext) -> None:
    tid, token = create_trigger(client, ctx, rate_limit_per_minute=2)
    payload = {"client_payload": {"branch": "main"}}
    codes = [
        post(client, tid, token, payload, **{"Idempotency-Key": str(i)}).status_code
        for i in range(3)
    ]
    assert codes == [202, 202, 429]
    client.patch(f"/api/v1/triggers/{tid}", json={"enabled": False, "rate_limit_per_minute": 100})
    disabled = post(client, tid, token, payload, **{"Idempotency-Key": "z"})
    assert disabled.status_code == 403


def test_webhooks_are_public_when_the_api_requires_a_token(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path, require_token=True, api_token_hash=hash_token("api-token-value")
    )
    ctx = build_context(settings, migrate=True)
    try:
        app = create_app(ctx, web_dist=tmp_path / "none")
        with TestClient(app, base_url="http://127.0.0.1") as c:
            api_auth = {"Authorization": "Bearer api-token-value"}
            assert c.get("/api/v1/triggers").status_code == 401
            prompt = branch_prompt(ctx)
            profile = demo_profile(ctx)
            created = c.post(
                "/api/v1/triggers",
                json={
                    "name": "t",
                    "prompt_template_id": prompt,
                    "agent_profile_id": profile,
                    "input_mapping": {"branch": "$.b"},
                },
                headers=api_auth,
            ).json()
            tid, token = created["trigger"]["id"], created["token"]
            # The webhook authenticates with its own token, not the API token.
            assert (
                c.post(
                    f"/api/v1/webhooks/{tid}", content=b'{"b":"x"}', headers={**JSON, **api_auth}
                ).status_code
                == 401
            )
            ok = c.post(
                f"/api/v1/webhooks/{tid}",
                content=b'{"b":"x"}',
                headers={**JSON, "Authorization": f"Bearer {token}"},
            )
            assert ok.status_code == 202
    finally:
        ctx.close()


@pytest.fixture
def two_clients(ctx: AppContext) -> Iterator[TestClient]:
    app = create_app(ctx, web_dist=ctx.settings.data_dir / "none")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        yield c


def test_concurrent_identical_deliveries_make_one_run(
    two_clients: TestClient, ctx: AppContext
) -> None:
    import threading

    tid, token = create_trigger(two_clients, ctx, rate_limit_per_minute=100)
    results: list[int] = []

    def send() -> None:
        r = post(
            two_clients,
            tid,
            token,
            {"client_payload": {"branch": "m"}},
            **{"Idempotency-Key": "same"},
        )
        results.append(r.status_code)

    threads = [threading.Thread(target=send) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results).count(202) == 1
    with ctx.session() as session:
        assert len(session.scalars(select(Run).where(Run.trigger_id == tid)).all()) == 1
