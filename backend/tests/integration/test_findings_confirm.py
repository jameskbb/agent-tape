"""Confirm and withdraw confirmation of a finding: the
one human rung on the evidence ladder, derived entirely from the append-only audit log -
no migration, no stored column."""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agenttape.application.context import AppContext
from agenttape.application.services import findings as findings_module
from agenttape.application.services.findings import (
    CONFIRM_ACTION,
    WITHDRAW_ACTION,
    FindingsService,
)
from agenttape.domain.errors import ConflictError, NotFoundError
from agenttape.infrastructure.db.base import new_id
from agenttape.infrastructure.db.extra_models.findings import Finding
from agenttape.infrastructure.db.models import AuditEntry, Run
from tests.conftest import demo_profile, start_run
from tests.integration.helpers import race_two_contexts, run_findings
from tests.integration.test_context_evidence import LEAK, run_with


def _finding(client: TestClient, run_id: str, key: str) -> dict[str, object]:
    body = client.get(f"/api/v1/runs/{run_id}/context/findings").json()
    [entry] = [f for f in body if f["key"] == key]
    return entry


def test_confirm_records_audit_and_appears_on_the_findings_response(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    run_id = run_with(
        ctx,
        repo_project,
        [
            {"type": "finding", "payload": {"id": "F1", "text": "checked the cited lines"}},
            {"type": "finding", "payload": {"id": "F2", "text": "a second, unconfirmed finding"}},
        ],
    )
    f1 = _finding(client, run_id, "F1")
    assert f1["confirmation"] is None

    resp = client.post(
        f"/api/v1/findings/{f1['id']}/confirm",
        json={"note": "checked the cited lines"},
        headers={"X-AgentTape-Actor": "James"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["finding_id"] == f1["id"]
    assert body["confirmation"]["actor"] == "James"
    assert body["confirmation"]["note"] == "checked the cited lines"
    assert body["confirmation"]["ts"]

    findings_after = client.get(f"/api/v1/runs/{run_id}/context/findings").json()
    confirmed = next(f for f in findings_after if f["key"] == "F1")
    unconfirmed = next(f for f in findings_after if f["key"] == "F2")
    assert confirmed["confirmation"]["actor"] == "James"
    assert unconfirmed["confirmation"] is None


def test_confirm_appends_a_finding_confirm_audit_entry(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    run_id = run_with(
        ctx, repo_project, [{"type": "finding", "payload": {"id": "F1", "text": "x"}}]
    )
    finding = _finding(client, run_id, "F1")
    resp = client.post(
        f"/api/v1/findings/{finding['id']}/confirm",
        json={"note": "looks right"},
        headers={"X-AgentTape-Actor": "James"},
    )
    assert resp.status_code == 200, resp.text

    entries = client.get("/api/v1/audit", params={"resource_type": "finding"}).json()["items"]
    [entry] = [e for e in entries if e["action"] == CONFIRM_ACTION]
    assert entry["resource_id"] == finding["id"]
    assert entry["run_id"] == run_id
    assert entry["actor"] == "James"
    assert entry["details"]["note"] == "looks right"


def test_withdraw_then_reconfirm_never_touches_the_finding_or_its_verdicts(
    ctx: AppContext, client: TestClient, repo_project: str
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
                    "text": "claim",
                    "links": [
                        {"path": "src/greeter.py", "line_start": 1, "line_end": 2, "quote": quote}
                    ],
                },
            }
        ],
    )
    finding_before = _finding(client, run_id, "F1")
    client.post(
        f"/api/v1/findings/{finding_before['id']}/confirm",
        json={},
        headers={"X-AgentTape-Actor": "James"},
    )

    resp = client.post(
        f"/api/v1/findings/{finding_before['id']}/confirm/withdraw",
        json={},
        headers={"X-AgentTape-Actor": "James"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"finding_id": finding_before["id"], "confirmation": None}

    entries = client.get("/api/v1/audit", params={"resource_type": "finding"}).json()["items"]
    actions = [e["action"] for e in entries if e["resource_id"] == finding_before["id"]]
    assert actions.count(WITHDRAW_ACTION) == 1

    finding_after = _finding(client, run_id, "F1")
    assert finding_after["confirmation"] is None
    assert finding_after["text"] == finding_before["text"]
    assert finding_after["citations"] == finding_before["citations"]

    # A second confirm after the withdraw succeeds (the withdraw row is not a permanent lock).
    resp2 = client.post(
        f"/api/v1/findings/{finding_before['id']}/confirm",
        json={},
        headers={"X-AgentTape-Actor": "James"},
    )
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["confirmation"]["actor"] == "James"


def test_confirm_twice_conflicts(ctx: AppContext, client: TestClient, repo_project: str) -> None:
    run_id = run_with(
        ctx, repo_project, [{"type": "finding", "payload": {"id": "F1", "text": "x"}}]
    )
    finding = _finding(client, run_id, "F1")
    first = client.post(f"/api/v1/findings/{finding['id']}/confirm", json={})
    assert first.status_code == 200, first.text
    second = client.post(f"/api/v1/findings/{finding['id']}/confirm", json={})
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "conflict"
    assert _finding(client, run_id, "F1")["confirmation"] is not None
    with ctx.session() as session:
        rows = session.scalars(
            select(AuditEntry).where(
                AuditEntry.resource_id == finding["id"], AuditEntry.action == CONFIRM_ACTION
            )
        ).all()
    assert len(rows) == 1


def test_withdraw_without_a_confirmation_conflicts(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    run_id = run_with(
        ctx, repo_project, [{"type": "finding", "payload": {"id": "F1", "text": "x"}}]
    )
    finding = _finding(client, run_id, "F1")
    resp = client.post(f"/api/v1/findings/{finding['id']}/confirm/withdraw", json={})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"
    assert _finding(client, run_id, "F1")["confirmation"] is None


def test_confirm_and_withdraw_404_for_an_unknown_finding(client: TestClient) -> None:
    assert client.post("/api/v1/findings/does-not-exist/confirm", json={}).status_code == 404
    assert (
        client.post("/api/v1/findings/does-not-exist/confirm/withdraw", json={}).status_code == 404
    )


def test_confirm_requires_a_finished_run(ctx: AppContext, client: TestClient) -> None:
    """A finding on a run that has not finished cannot be confirmed yet - there is nothing
    settled to review. The finding is inserted directly via ORM (bypassing the worker, like
    ``test_direct_orm_inserts_are_redacted_by_the_guard`` in test_findings.py) since a queued
    run's adapter has not run yet and so has recorded no findings of its own."""
    profile_id = demo_profile(ctx, "success")
    run_id = start_run(ctx, profile_id)
    with ctx.session() as session:
        run = session.get(Run, run_id)
        assert run is not None
        assert run.status == "queued"
        # fixture only: production code writes these rows through FindingsService
        finding = Finding(
            run_id=run_id,
            attempt_id=None,
            node_key=None,
            origin_event_id=new_id(),
            sequence=0,
            finding_key="F1",
            kind="claim",
            text="claim",
            source="test",
            details={},
            created_at=ctx.clock.now(),
        )
        session.add(finding)
        session.flush()
        finding_id = finding.id

    resp = client.post(f"/api/v1/findings/{finding_id}/confirm", json={})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "run_not_finished"


def test_confirm_note_over_2000_characters_is_rejected(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    run_id = run_with(
        ctx, repo_project, [{"type": "finding", "payload": {"id": "F1", "text": "x"}}]
    )
    finding = _finding(client, run_id, "F1")
    resp = client.post(f"/api/v1/findings/{finding['id']}/confirm", json={"note": "x" * 2001})
    assert resp.status_code == 422


def test_confirmation_note_is_redacted_and_never_echoed_unredacted(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    run_id = run_with(
        ctx, repo_project, [{"type": "finding", "payload": {"id": "F1", "text": "x"}}]
    )
    finding = _finding(client, run_id, "F1")
    resp = client.post(
        f"/api/v1/findings/{finding['id']}/confirm",
        json={"note": f"the key is {LEAK}"},
        headers={"X-AgentTape-Actor": "James"},
    )
    assert resp.status_code == 200, resp.text
    assert LEAK not in json.dumps(resp.json())
    assert "[REDACTED:" in (resp.json()["confirmation"]["note"] or "")

    with ctx.session() as session:
        entry = session.scalars(
            select(AuditEntry).where(
                AuditEntry.resource_type == "finding",
                AuditEntry.resource_id == finding["id"],
                AuditEntry.action == CONFIRM_ACTION,
            )
        ).one()
        assert LEAK not in json.dumps(entry.details)
        assert "[REDACTED:" in (entry.details.get("note") or "")

    findings_after = client.get(f"/api/v1/runs/{run_id}/context/findings").json()
    entry_after = next(f for f in findings_after if f["key"] == "F1")
    assert LEAK not in json.dumps(entry_after)
    assert "[REDACTED:" in (entry_after["confirmation"]["note"] or "")


def test_service_confirm_and_withdraw_raise_the_same_errors_directly(
    ctx: AppContext, repo_project: str
) -> None:
    """Exercises FindingsService.confirm/withdraw_confirmation directly (not just through the
    HTTP layer), since the service is the one write path."""
    run_id = run_with(
        ctx, repo_project, [{"type": "finding", "payload": {"id": "F1", "text": "x"}}]
    )
    service = FindingsService(ctx)
    [finding] = run_findings(ctx, run_id)
    with pytest.raises(NotFoundError):
        service.confirm("does-not-exist", actor="James")
    with pytest.raises(NotFoundError):
        service.withdraw_confirmation("does-not-exist", actor="James")
    result = service.confirm(finding["id"], actor="James", note="ok")
    assert result["confirmation"]["actor"] == "James"
    with pytest.raises(ConflictError):
        service.confirm(finding["id"], actor="James")
    service.withdraw_confirmation(finding["id"], actor="James")
    with pytest.raises(ConflictError):
        service.withdraw_confirmation(finding["id"], actor="James")


@pytest.mark.parametrize("action", ["confirm", "withdraw"])
def test_concurrent_requests_append_only_one_audit_row(
    ctx: AppContext,
    client: TestClient,
    repo_project: str,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    """Two requests racing through two contexts both pass the service's own
    "not yet confirmed" (or "is confirmed") check before either appends (a barrier holds
    them there). Exactly one audit row lands; the loser gets the same 409 conflict a
    sequential second request does, and its row is rolled back."""
    run_id = run_with(
        ctx, repo_project, [{"type": "finding", "payload": {"id": "F1", "text": "racy"}}]
    )
    finding_id = str(_finding(client, run_id, "F1")["id"])
    service = FindingsService(ctx)
    if action == "withdraw":
        service.confirm(finding_id, actor="setup")
    wanted = CONFIRM_ACTION if action == "confirm" else WITHDRAW_ACTION

    barrier = threading.Barrier(2, timeout=15)
    original = findings_module.record_audit

    def append_after_both_checked(*args: Any, **kwargs: Any) -> Any:
        # PostgreSQL's row lock holds the second request before its pre-check, so it can
        # never reach a barrier here; the two simply race for that lock.
        if ctx.settings.is_sqlite:
            barrier.wait()  # both requests have already passed the pre-check
        return original(*args, **kwargs)

    monkeypatch.setattr(findings_module, "record_audit", append_after_both_checked)

    def request(c: AppContext) -> Any:
        svc = FindingsService(c)
        actor = "Ana" if c is ctx else "Ben"
        if action == "confirm":
            return svc.confirm(finding_id, actor=actor)
        return svc.withdraw_confirmation(finding_id, actor=actor)

    outcomes = race_two_contexts(ctx, request)
    kinds = sorted(kind for kind, _ in outcomes)
    assert kinds == ["conflict", "ok"], outcomes
    with ctx.session() as session:
        rows = session.scalars(
            select(AuditEntry).where(
                AuditEntry.resource_type == "finding",
                AuditEntry.resource_id == finding_id,
                AuditEntry.action == wanted,
            )
        ).all()
        assert len(rows) == 1, [(r.action, r.actor) for r in rows]

    # The derived state is the winner's, and a sequential repeat still conflicts.
    monkeypatch.setattr(findings_module, "record_audit", original)
    with pytest.raises(ConflictError):
        if action == "confirm":
            service.confirm(finding_id, actor="Cy")
        else:
            service.withdraw_confirmation(finding_id, actor="Cy")
