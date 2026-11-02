"""The verification engine's end-to-end tests.

A finished run's recorded ``Read`` tool call backs a real ``quote_matches`` verdict, with
the true line range recomputed from the content the run actually received, appended
through ``FindingsService`` with ``method="lexical_v1"``, and visible through the existing
findings endpoint. Unfinished runs are refused; a citation is never supported by a read
recorded in a different run.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agenttape.application.context import AppContext
from agenttape.application.events import EventRecorder
from agenttape.application.schemas.catalog import AgentProfileCreate
from agenttape.application.schemas.runs import RunCreate
from agenttape.application.services.findings import FindingsService
from agenttape.application.services.profiles import AgentProfileService
from agenttape.application.services.runs import RunService
from agenttape.application.services.verification import VerificationService
from agenttape.domain.enums import EventType, RunStatus
from agenttape.domain.errors import ValidationError
from agenttape.findings.verify import NOT_CHECKABLE_REASONS
from agenttape.infrastructure.db.extra_models.findings import CitationVerdict
from agenttape.infrastructure.db.extra_models.verification import EvidenceBlob
from agenttape.infrastructure.db.models import Event, RunAttempt
from tests.integration.capture_helpers import (
    hooks_token,
    loopback_client,
    post_hook,
    post_read,
    pre_read,
    session_end,
    session_start,
    svc_project,
)
from tests.integration.helpers import (
    GREETER_TEXT,
    OTHER_TEXT,
    command_record,
    file_write_record,
    finding_record,
    findings_of,
    link_finding,
    read_records,
)
from tests.integration.test_context_evidence import LEAK, ScriptedAdapter, run_with

SESSION_ID = "sess-verify-hooks"


def _queued_run(ctx: AppContext, project_id: str) -> str:
    """A run created but never driven: it stays ``queued``, never reaching a terminal status."""
    if not ctx.adapters.has("test_context"):
        ctx.adapters.register(ScriptedAdapter())
    profile = AgentProfileService(ctx).create(
        AgentProfileCreate(
            name="verify-unfinished",
            adapter_type="test_context",
            config={"records": [], "model": "fixture-model"},
        )
    )
    result = RunService(ctx).create_run(
        RunCreate(agent_profile_id=profile.id, project_id=project_id, prompt="Do nothing yet")
    )
    return result.run.id


def test_a_finished_run_is_verified_without_a_request(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    """The worker verifies a run's citations automatically once it finishes - no POST."""
    quote = "def greet():\n    return 'hi'"
    records = [
        *read_records("toolu_auto", "src/greeter.py", GREETER_TEXT),
        finding_record("F1", "src/greeter.py", 7, 8, quote),
    ]
    run_id = run_with(ctx, repo_project, records)  # drives the run to "succeeded" already.

    [entry] = [f for f in findings_of(client, run_id) if f["key"] == "F1"]
    [citation] = entry["citations"]
    verdict = citation["verdict"]
    assert verdict is not None
    assert verdict["verdict"] == "quote_matches"
    assert verdict["method"] == "lexical_v1"
    assert citation["checks"] == 1


def test_quote_matches_recomputes_the_true_line_range(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    quote = "def greet():\n    return 'hi'"
    records = [
        *read_records("toolu_r1", "src/greeter.py", GREETER_TEXT),
        finding_record("F1", "src/greeter.py", 7, 8, quote),
    ]
    run_id = run_with(ctx, repo_project, records)

    resp = client.post(f"/api/v1/runs/{run_id}/findings/verify")
    assert resp.status_code == 200, resp.text

    [entry] = [f for f in findings_of(client, run_id) if f["key"] == "F1"]
    [citation] = entry["citations"]
    verdict = citation["verdict"]
    assert verdict is not None
    assert verdict["verdict"] == "quote_matches"
    assert verdict["method"] == "lexical_v1"
    assert verdict["verifier_version"] == "2"
    evidence = verdict["evidence"]
    assert evidence["line_range"] == [1, 2]
    assert evidence["claimed_range"] == [7, 8]
    assert evidence["claimed_range_matches"] is False

    with ctx.session() as session:
        result_event_id = session.scalars(
            select(Event.id).where(
                Event.run_id == run_id, Event.type == EventType.TOOL_RESULT.value
            )
        ).one()
    assert evidence["read"]["result_event_id"] == result_event_id


def test_verify_refuses_an_unfinished_run(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    run_id = _queued_run(ctx, repo_project)
    resp = client.post(f"/api/v1/runs/{run_id}/findings/verify")
    assert resp.status_code == 409, resp.text


def test_stale_citation_shows_change_event(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    quote = "def greet():\n    return 'hi'"
    records = [
        *read_records("toolu_r1", "src/greeter.py", GREETER_TEXT),
        file_write_record("src/greeter.py", "modify", 40),
        finding_record("F1", "src/greeter.py", 1, 2, quote),
    ]
    run_id = run_with(ctx, repo_project, records)

    resp = client.post(f"/api/v1/runs/{run_id}/findings/verify")
    assert resp.status_code == 200, resp.text

    [entry] = [f for f in findings_of(client, run_id) if f["key"] == "F1"]
    [citation] = entry["citations"]
    verdict = citation["verdict"]
    assert verdict is not None
    assert verdict["verdict"] == "stale"
    evidence = verdict["evidence"]
    assert evidence["line_range"] == [1, 2]
    assert evidence["later_writes"] >= 1
    assert evidence["change"]["operation"] == "modify"

    with ctx.session() as session:
        write_event_id = session.scalars(
            select(Event.id).where(Event.run_id == run_id, Event.type == EventType.FILE_WRITE.value)
        ).one()
    assert evidence["change"]["source_event_id"] == write_event_id
    assert "hash_at_read" in evidence
    assert evidence["hash_at_read"] is None or (
        isinstance(evidence["hash_at_read"], str) and len(evidence["hash_at_read"]) == 40
    )
    assert "hash_after" in evidence
    assert evidence["hash_after"] is None or (
        isinstance(evidence["hash_after"], str) and len(evidence["hash_after"]) == 40
    )


def test_a_write_before_the_read_is_not_staleness(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    quote = "def greet():\n    return 'hi'"
    records = [
        file_write_record("src/greeter.py", "modify", 40),
        *read_records("toolu_r1", "src/greeter.py", GREETER_TEXT),
        finding_record("F1", "src/greeter.py", 1, 2, quote),
    ]
    run_id = run_with(ctx, repo_project, records)

    resp = client.post(f"/api/v1/runs/{run_id}/findings/verify")
    assert resp.status_code == 200, resp.text

    [entry] = [f for f in findings_of(client, run_id) if f["key"] == "F1"]
    [citation] = entry["citations"]
    verdict = citation["verdict"]
    assert verdict is not None
    assert verdict["verdict"] == "quote_matches"


def test_a_read_in_another_run_never_supports_a_citation(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    quote = "def greet():\n    return 'hi'"
    run_a = run_with(ctx, repo_project, read_records("toolu_a1", "src/greeter.py", GREETER_TEXT))
    run_b = run_with(ctx, repo_project, [finding_record("F1", "src/greeter.py", 1, 2, quote)])
    assert run_a != run_b

    resp = client.post(f"/api/v1/runs/{run_b}/findings/verify")
    assert resp.status_code == 200, resp.text

    [entry] = [f for f in findings_of(client, run_b) if f["key"] == "F1"]
    [citation] = entry["citations"]
    verdict = citation["verdict"]
    assert verdict is None or verdict["verdict"] != "quote_matches"


def _runtime_reconnect_record(offline_from: str, offline_to: str) -> dict[str, Any]:
    return {
        "type": "runtime",
        "payload": {
            "action": "reconnect",
            "offline_from": offline_from,
            "offline_to": offline_to,
            "runtime": "herdr",
        },
    }


def test_never_read_vs_read_not_observed_on_recorded_runs(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    quote = "def greet():"

    # Run A: a read of another file, complete window - never_read.
    run_a = run_with(
        ctx,
        repo_project,
        [
            *read_records("toolu_a", "src/other.py", OTHER_TEXT),
            finding_record("F1", "src/greeter.py", 1, 1, quote),
        ],
    )
    resp_a = client.post(f"/api/v1/runs/{run_a}/findings/verify")
    assert resp_a.status_code == 200, resp_a.text
    [entry_a] = [f for f in findings_of(client, run_a) if f["key"] == "F1"]
    verdict_a = entry_a["citations"][0]["verdict"]
    assert verdict_a["verdict"] == "never_read"

    # Run B: no reads anywhere in the run - read telemetry itself is unavailable.
    run_b = run_with(ctx, repo_project, [finding_record("F1", "src/greeter.py", 1, 1, quote)])
    resp_b = client.post(f"/api/v1/runs/{run_b}/findings/verify")
    assert resp_b.status_code == 200, resp_b.text
    [entry_b] = [f for f in findings_of(client, run_b) if f["key"] == "F1"]
    verdict_b = entry_b["citations"][0]["verdict"]
    assert verdict_b["verdict"] == "read_not_observed"
    assert verdict_b["evidence"]["window"]["reads"] == "unavailable"

    # Run C: a read exists, but AgentTape was offline for part of the run.
    run_c = run_with(
        ctx,
        repo_project,
        [
            *read_records("toolu_c", "src/other.py", OTHER_TEXT),
            _runtime_reconnect_record("2026-01-01T00:00:00Z", "2026-01-01T00:05:00Z"),
            finding_record("F1", "src/greeter.py", 1, 1, quote),
        ],
    )
    resp_c = client.post(f"/api/v1/runs/{run_c}/findings/verify")
    assert resp_c.status_code == 200, resp_c.text
    [entry_c] = [f for f in findings_of(client, run_c) if f["key"] == "F1"]
    verdict_c = entry_c["citations"][0]["verdict"]
    assert verdict_c["verdict"] == "read_not_observed"
    assert verdict_c["evidence"]["window"]["offline_windows"]

    # Run D: a partial read whose returned window (line 2 only) does not cover the claim.
    run_d = run_with(
        ctx,
        repo_project,
        [
            *read_records("toolu_d", "src/greeter.py", GREETER_TEXT, offset=2, limit=1),
            finding_record("F1", "src/greeter.py", 1, 1, quote),
        ],
    )
    resp_d = client.post(f"/api/v1/runs/{run_d}/findings/verify")
    assert resp_d.status_code == 200, resp_d.text
    [entry_d] = [f for f in findings_of(client, run_d) if f["key"] == "F1"]
    verdict_d = entry_d["citations"][0]["verdict"]
    assert verdict_d["verdict"] == "never_read"
    assert "2-2" in verdict_d["reason"]


def test_not_checkable_reasons(
    ctx: AppContext, client: TestClient, repo_project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The 2,000-line read is about 30 KB, over the 16 KiB event text cap but under the
    # 1 MiB retained-content cap it is checked against; lower that cap so line 1999 is
    # past it.
    monkeypatch.setattr("agenttape.findings.retained_output.MAX_RETAINED_BYTES", 8 * 1024)
    big_text = "\n".join(f"line {i}" for i in range(1, 2001))
    records = [
        {
            "type": "file_read",
            "payload": {"path": "src/other.py", "bytes": len(OTHER_TEXT.encode("utf-8"))},
        },
        *read_records("toolu_secret", "src/secret.py", f"line one\n{LEAK}"),
        *read_records("toolu_big", "src/big.py", big_text),
        finding_record("Fa", "src/other.py", 1, 1, "X = 1"),
        finding_record("Fb", "src/secret.py", 1, 2, f"line one\n{LEAK}"),
        finding_record("Fc0", "src/big.py", 1999, 1999, "line 1999"),
        finding_record("Fc1", "src/big.py", 1, 1, "line 1"),
        link_finding("Fd", [{"path": "../../etc/hosts", "quote": "root:x:0:0"}]),
        link_finding("Fe", [{"path": "src/other.py", "line_start": 1}]),
    ]
    run_id = run_with(ctx, repo_project, records)
    resp = client.post(f"/api/v1/runs/{run_id}/findings/verify")
    assert resp.status_code == 200, resp.text
    findings = {f["key"]: f for f in findings_of(client, run_id)}

    def verdict_of(key: str) -> dict[str, Any]:
        [citation] = findings[key]["citations"]
        verdict = citation["verdict"]
        assert verdict is not None, key
        return verdict

    assert verdict_of("Fa")["evidence"]["not_checkable_reason"] == "no_content_recorded"
    assert verdict_of("Fb")["evidence"]["not_checkable_reason"] == "redacted_at_cited_span"
    assert verdict_of("Fc0")["evidence"]["not_checkable_reason"] == "size_cap_exceeded"
    assert verdict_of("Fc1")["verdict"] == "quote_matches"
    assert verdict_of("Fd")["evidence"]["not_checkable_reason"] == "outside_boundary"
    assert verdict_of("Fe")["evidence"]["not_checkable_reason"] == "no_quote"


def test_quote_not_found_when_the_read_covers_the_lines(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    records = [
        *read_records("toolu_g1", "src/greeter.py", GREETER_TEXT),
        finding_record("F1", "src/greeter.py", 1, 1, "def greet(name):"),
    ]
    run_id = run_with(ctx, repo_project, records)
    resp = client.post(f"/api/v1/runs/{run_id}/findings/verify")
    assert resp.status_code == 200, resp.text
    [entry] = [f for f in findings_of(client, run_id) if f["key"] == "F1"]
    assert entry["citations"][0]["verdict"]["verdict"] == "quote_not_found"


def test_event_citation_verified(ctx: AppContext, client: TestClient, repo_project: str) -> None:
    records = [
        command_record("cmd_1", ["python3", "-m", "compileall", "-q", "src"], "2 checks passed"),
        *read_records("toolu_e1", "src/other.py", OTHER_TEXT),
        link_finding("F1", [{"event_ref": "cmd_1", "quote": "2 checks passed"}]),
        link_finding("F2", [{"event_ref": "cmd_1", "quote": "3 checks passed"}]),
        link_finding("F3", [{"event_ref": "cmd_missing", "quote": "2 checks passed"}]),
    ]
    run_id = run_with(ctx, repo_project, records)
    resp = client.post(f"/api/v1/runs/{run_id}/findings/verify")
    assert resp.status_code == 200, resp.text
    findings = {f["key"]: f for f in findings_of(client, run_id)}

    with ctx.session() as session:
        command_event_id = session.scalars(
            select(Event.id).where(Event.run_id == run_id, Event.type == EventType.COMMAND.value)
        ).one()

    f1_verdict = findings["F1"]["citations"][0]["verdict"]
    assert f1_verdict["verdict"] == "quote_matches"
    assert f1_verdict["evidence"]["event"]["event_id"] == command_event_id

    f2_verdict = findings["F2"]["citations"][0]["verdict"]
    assert f2_verdict["verdict"] == "quote_not_found"

    f3_verdict = findings["F3"]["citations"][0]["verdict"]
    assert f3_verdict["verdict"] == "never_read"


@pytest.mark.slow
def test_demo_evidence_run_verdicts_are_computed_by_the_engine(
    ctx: AppContext, client: TestClient
) -> None:
    """Honesty check on the seeded evidence demo run: the real verifier, not a hand-authored
    seed, must independently arrive at every designed verdict (except F7's documented
    stand-in) from the run's own recorded evidence."""
    from agenttape import demo_agent
    from agenttape.seeds import file_evidence_demo
    from agenttape.seeds.base import SeedReport
    from tests.conftest import drive, start_run

    report = SeedReport()
    project_id = file_evidence_demo._project(ctx, report)
    profile_id = file_evidence_demo._profiles(ctx, report, project_id)["a"]
    run_id = start_run(ctx, profile_id, project_id=project_id)
    assert drive(ctx, run_id, timeout=120).run.status == RunStatus.WAITING_FOR_APPROVAL
    assert file_evidence_demo.approve_evidence_runs(ctx, [run_id]) == 1
    assert drive(ctx, run_id, timeout=120).run.status == RunStatus.SUCCEEDED

    resp = client.post(f"/api/v1/runs/{run_id}/findings/verify")
    assert resp.status_code == 200, resp.text

    findings = {f["key"]: f for f in findings_of(client, run_id)}

    for (finding_id, position), expected in demo_agent.DESIGNED_VERDICTS.items():
        citation = findings[finding_id]["citations"][position]
        verdict = citation["verdict"]
        assert verdict is not None, f"{finding_id}[{position}] has no verdict"
        if (finding_id, position) == ("F7", 0):
            assert verdict["verdict"] == "never_read", verdict
            continue
        assert verdict["verdict"] == expected, (finding_id, position, verdict["verdict"])
        assert verdict["method"] == "lexical_v1"

    f2_evidence = findings["F2"]["citations"][0]["verdict"]["evidence"]
    assert f2_evidence["hash_at_read_matches_recorded"] is True
    assert isinstance(f2_evidence["hash_at_read"], str) and len(f2_evidence["hash_at_read"]) == 40
    assert isinstance(f2_evidence["hash_after"], str) and len(f2_evidence["hash_after"]) == 40
    assert f2_evidence["hash_at_read"] != f2_evidence["hash_after"]

    f3_event_verdict = findings["F3"]["citations"][1]["verdict"]
    assert f3_event_verdict["evidence"]["event"]["content_key"] == "output"

    for finding_id in demo_agent.DECLARED_ONLY_FINDINGS:
        entry = findings[finding_id]
        assert entry["declared_only"] is True
        assert entry["citations"] == []


def test_rerun_is_idempotent(ctx: AppContext, client: TestClient, repo_project: str) -> None:
    """An automatic pass on unchanged evidence records nothing; explicit re-checks always
    append, with equal verdict/reason/evidence (the same ``inputs_digest``) and a kept
    ``rerun_of_id`` chain."""
    quote = "def greet():\n    return 'hi'"
    records = [
        *read_records("toolu_idem", "src/greeter.py", GREETER_TEXT),
        finding_record("F1", "src/greeter.py", 1, 2, quote),
    ]
    run_id = run_with(ctx, repo_project, records)  # the automatic pass already ran once.

    [entry] = [f for f in findings_of(client, run_id) if f["key"] == "F1"]
    [citation] = entry["citations"]
    assert citation["checks"] == 1

    summary = VerificationService(ctx).verify_run(run_id, trigger="automatic")
    assert summary.recorded == 0
    assert summary.unchanged == 1

    [entry] = [f for f in findings_of(client, run_id) if f["key"] == "F1"]
    assert entry["citations"][0]["checks"] == 1

    resp1 = client.post(f"/api/v1/runs/{run_id}/findings/verify")
    assert resp1.status_code == 200, resp1.text
    resp2 = client.post(f"/api/v1/runs/{run_id}/findings/verify")
    assert resp2.status_code == 200, resp2.text

    [entry] = [f for f in findings_of(client, run_id) if f["key"] == "F1"]
    citation = entry["citations"][0]
    assert citation["checks"] == 3

    with ctx.session() as session:
        rows = list(
            session.scalars(
                select(CitationVerdict)
                .where(CitationVerdict.citation_id == citation["id"])
                .order_by(CitationVerdict.created_at, CitationVerdict.id)
            )
        )
    assert len(rows) == 3
    first, second, third = rows
    assert {row.verdict for row in rows} == {"quote_matches"}
    assert first.reason == second.reason == third.reason
    assert first.evidence == second.evidence == third.evidence
    assert first.evidence.get("inputs_digest")
    assert second.rerun_of_id == first.id
    assert third.rerun_of_id == second.id


def test_changed_evidence_gets_a_new_automatic_check(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    """A read retained as a local copy (a file outside the repository): deleting the
    retained file changes the citation's recorded inputs, so the next automatic pass
    records a new check instead of skipping it as unchanged."""
    records = [
        *read_records("toolu_notes_auto", "src/notes.py", "line one\nline two\nline three\n"),
        finding_record("F1", "src/notes.py", 2, 2, "line two"),
    ]
    run_id = run_with(ctx, repo_project, records)

    [entry] = [f for f in findings_of(client, run_id) if f["key"] == "F1"]
    [citation] = entry["citations"]
    assert citation["checks"] == 1
    assert citation["verdict"]["verdict"] == "quote_matches"

    with ctx.session() as session:
        blob = session.scalars(select(EvidenceBlob).where(EvidenceBlob.run_id == run_id)).one()
        storage_path = blob.storage_path
    assert storage_path is not None
    ctx.artifact_store.resolve(storage_path).unlink()

    summary = VerificationService(ctx).verify_run(run_id, trigger="automatic")
    assert summary.recorded == 1
    assert summary.unchanged == 0

    [entry] = [f for f in findings_of(client, run_id) if f["key"] == "F1"]
    citation = entry["citations"][0]
    assert citation["checks"] == 2
    assert citation["verdict"]["verdict"] == "not_checkable"
    assert citation["verdict"]["evidence"]["not_checkable_reason"] == "blob_unavailable"


@pytest.mark.slow
def test_verification_waits_for_the_run_to_finish(ctx: AppContext, client: TestClient) -> None:
    """A run paused for approval has no automatic verdicts yet; once it finishes, every
    citation has exactly one ``lexical_v1`` row."""
    from agenttape.seeds import file_evidence_demo
    from agenttape.seeds.base import SeedReport
    from tests.conftest import drive, start_run

    report = SeedReport()
    project_id = file_evidence_demo._project(ctx, report)
    profile_id = file_evidence_demo._profiles(ctx, report, project_id)["a"]
    run_id = start_run(ctx, profile_id, project_id=project_id)
    assert drive(ctx, run_id, timeout=120).run.status == RunStatus.WAITING_FOR_APPROVAL

    paused = findings_of(client, run_id)
    paused_citations = [c for f in paused for c in f["citations"]]
    assert paused_citations  # citations already exist (ingested on the approval pause too)
    assert all(c["verdict"] is None for c in paused_citations)

    assert file_evidence_demo.approve_evidence_runs(ctx, [run_id]) == 1
    assert drive(ctx, run_id, timeout=120).run.status == RunStatus.SUCCEEDED

    findings = findings_of(client, run_id)
    citations = [c for f in findings for c in f["citations"]]
    assert citations
    for citation in citations:
        verdict = citation["verdict"]
        assert verdict is not None, citation
        assert verdict["method"] == "lexical_v1"
        assert citation["checks"] == 1


def test_an_observed_run_is_verified_automatically_on_close(ctx: AppContext) -> None:
    """An observed run (Claude Code ``http`` hooks) closes from
    ``SessionEnd`` through ``ObservedSessionService``/``fire_run_terminal`` - never a
    worker executor - but that path is the exact same ``worker.hooks.run_terminal_hooks()``
    an ordinary run's completion fires, so its citations get verified automatically too."""
    project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)
    file_path = str(repo / "src" / "greeter.py")

    assert (
        post_hook(client, token, session_start(str(repo), session_id=SESSION_ID)).status_code == 200
    )
    assert (
        post_hook(client, token, pre_read(str(repo), file_path, session_id=SESSION_ID)).status_code
        == 200
    )
    assert (
        post_hook(client, token, post_read(str(repo), file_path, session_id=SESSION_ID)).status_code
        == 200
    )

    runs = client.get("/api/v1/runs").json()["items"]
    [run] = [r for r in runs if r["project_id"] == project_id]
    run_id = run["id"]
    assert run["kind"] == "observed"

    with ctx.session() as session:
        attempt_id = session.scalars(select(RunAttempt.id).where(RunAttempt.run_id == run_id)).one()
        EventRecorder(ctx.settings, ctx.clock).record(
            session,
            run_id,
            EventType.FINDING,
            "F1",
            {
                "id": "F1",
                "kind": "claim",
                "text": "greet() returns a fixed string",
                "links": [
                    {
                        "path": "src/greeter.py",
                        "line_start": 1,
                        "line_end": 2,
                        "quote": "def greet():\n    return 'hi'",
                    }
                ],
            },
            attempt_id=attempt_id,
        )

    resp = post_hook(client, token, session_end(str(repo), session_id=SESSION_ID))
    assert resp.status_code == 200, resp.text

    findings = client.get(f"/api/v1/runs/{run_id}/context/findings").json()
    [entry] = [f for f in findings if f["key"] == "F1"]
    [citation] = entry["citations"]
    verdict = citation["verdict"]
    assert verdict is not None
    assert verdict["method"] == "lexical_v1"
    assert verdict["verdict"] == "quote_matches"
    assert citation["checks"] == 1


def test_record_verdict_refuses_an_unknown_not_checkable_reason(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    """A ``not_checkable`` verdict always names one of ``NOT_CHECKABLE_REASONS``: never an
    invented code, never a bare "not checkable" with no reason."""
    run_id = run_with(
        ctx, repo_project, [finding_record("F1", "src/greeter.py", 1, 1, "def greet():")]
    )
    [entry] = [f for f in findings_of(client, run_id) if f["key"] == "F1"]
    [citation] = entry["citations"]
    service = FindingsService(ctx)

    with pytest.raises(ValidationError):
        service.record_verdict(
            citation["id"],
            verdict="not_checkable",
            method="lexical",
            evidence={"not_checkable_reason": "made_up"},
        )
    with pytest.raises(ValidationError):
        service.record_verdict(citation["id"], verdict="not_checkable", method="lexical")

    row = service.record_verdict(
        citation["id"],
        verdict="not_checkable",
        method="lexical",
        evidence={"not_checkable_reason": NOT_CHECKABLE_REASONS[0]},
    )
    assert row["evidence"]["not_checkable_reason"] == NOT_CHECKABLE_REASONS[0]
