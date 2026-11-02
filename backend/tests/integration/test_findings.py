"""FindingsService: both citation shapes, event-reference resolution, redaction, caps, and
append-only citation verdicts, through the findings API and direct ORM access.

Legacy source-id links keep working unchanged, and a citation path outside the run's
working directory is recorded, never resolved on the filesystem."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from agenttape.application.context import AppContext
from agenttape.application.services.findings import FindingsService
from agenttape.context_evidence.hooks import rescan_run
from agenttape.domain.errors import NotFoundError, ValidationError
from agenttape.infrastructure.db.base import new_id
from agenttape.infrastructure.db.extra_models.findings import Citation, CitationVerdict, Finding
from agenttape.infrastructure.db.models import Event, Run
from tests.integration.test_context_evidence import LEAK, ctx_rec, run_with


def test_finding_with_a_file_citation_reaches_the_findings_api(
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
                    "kind": "claim",
                    "text": "greet() returns a fixed string",
                    "links": [
                        {
                            "path": "src/greeter.py",
                            "line_start": 1,
                            "line_end": 2,
                            "quote": quote,
                        }
                    ],
                },
            }
        ],
    )

    def get_findings() -> list[dict[str, object]]:
        return client.get(f"/api/v1/runs/{run_id}/context/findings").json()

    [entry] = [f for f in get_findings() if f["key"] == "F1"]
    assert entry["id"] is not None
    assert entry["declared_only"] is False
    [citation] = entry["citations"]
    assert citation["kind"] == "file"
    assert citation["path"] == "src/greeter.py"
    assert citation["inside_boundary"] is True
    assert citation["line_start"] == 1
    assert citation["line_end"] == 2
    assert citation["quote"] == quote
    # The worker verified this run's citations once, when it finished (which verdict is
    # test_findings_verify.py's concern).
    assert citation["checks"] == 1

    # Re-normalizing the same events (monitor, terminal rescan) never duplicates anything,
    # and never re-verifies (verification runs once, on run completion, not on every rescan).
    rescan_run(ctx, run_id)
    rescan_run(ctx, run_id)
    findings = get_findings()
    [entry_again] = [f for f in findings if f["key"] == "F1"]
    assert len(entry_again["citations"]) == 1
    assert entry_again["citations"][0]["checks"] == 1


def test_legacy_source_links_still_work_and_carry_no_citations(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    run_id = run_with(
        ctx,
        repo_project,
        [
            ctx_rec("retrieved", "kb:pricing", live=False),
            {
                "type": "finding",
                "payload": {
                    "id": "F1",
                    "text": "Price is 10",
                    "links": [{"source_id": "kb:pricing", "relation": "supports"}],
                },
            },
        ],
    )
    body = client.get(f"/api/v1/runs/{run_id}/context/findings").json()
    [entry] = [f for f in body if f["key"] == "F1"]
    assert len(entry["supports"]) == 1
    assert entry["citations"] == []
    assert entry["declared_only"] is True


def test_outside_path_is_recorded_not_resolved(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    run_id = run_with(
        ctx,
        repo_project,
        [
            {
                "type": "finding",
                "payload": {
                    "id": "F1",
                    "text": "Reads outside the project",
                    "links": [
                        {"path": "../../etc/hosts"},
                        {"path": "/etc/passwd"},
                    ],
                },
            }
        ],
    )
    body = client.get(f"/api/v1/runs/{run_id}/context/findings").json()
    [entry] = [f for f in body if f["key"] == "F1"]
    citations = sorted(entry["citations"], key=lambda c: c["position"])
    assert [c["path"] for c in citations] == ["<outside>/hosts", "<outside>/passwd"]
    assert all(c["inside_boundary"] is False for c in citations)


def test_event_citation_resolves_within_the_run_only(ctx: AppContext, repo_project: str) -> None:
    """A tool_use_id resolves through tool_result, then command, then tool_call; an
    AgentTape event id resolves directly; an unknown ref or one from another run stays
    unresolved."""
    run_id = run_with(
        ctx,
        repo_project,
        [
            {
                "type": "tool_call",
                "payload": {
                    "tool": "Read",
                    "input": {"file_path": "src/greeter.py"},
                    "tool_use_id": "toolu_1",
                },
            },
            {
                "type": "tool_result",
                "payload": {"tool_use_id": "toolu_1", "output": "hi", "is_error": False},
            },
            {"type": "command", "payload": {"argv": ["true"], "tool_use_id": "cmd_1"}},
        ],
    )
    other_run_id = run_with(ctx, repo_project, [], prompt="another run")

    with ctx.session() as session:
        run = session.get(Run, run_id)
        assert run is not None
        tool_result_id = session.scalars(
            select(Event.id).where(Event.run_id == run_id, Event.type == "tool_result")
        ).one()
        command_id = session.scalars(
            select(Event.id).where(Event.run_id == run_id, Event.type == "command")
        ).one()
        other_run_event_id = session.scalars(
            select(Event.id).where(Event.run_id == other_run_id)
        ).first()
        assert other_run_event_id is not None

        def finding_event(sequence: int, key: str, ref: str) -> Event:
            return Event(
                id=new_id(),
                run_id=run_id,
                sequence=sequence,
                ts=ctx.clock.now(),
                type="finding",
                source="test",
                severity="info",
                summary=key,
                payload={"id": key, "text": key, "links": [{"event_ref": ref, "quote": "q"}]},
            )

        cases = {
            "direct-id": finding_event(9001, "direct-id", tool_result_id),
            "via-tool-result": finding_event(9002, "via-tool-result", "toolu_1"),
            "via-command": finding_event(9003, "via-command", "cmd_1"),
            "unknown-ref": finding_event(9004, "unknown-ref", "does-not-exist"),
            "other-run-event": finding_event(9005, "other-run-event", other_run_event_id),
        }
        for event in cases.values():
            session.add(event)
        session.flush()
        resolved: dict[str, str | None] = {}
        for key, event in cases.items():
            finding = FindingsService.ingest_event(session, run, event, None)
            session.flush()
            [citation] = session.scalars(
                select(Citation).where(Citation.finding_id == finding.id)
            ).all()
            resolved[key] = citation.event_id

    assert resolved["direct-id"] == tool_result_id
    assert resolved["via-tool-result"] == tool_result_id
    assert resolved["via-command"] == command_id
    assert resolved["unknown-ref"] is None
    assert resolved["other-run-event"] is None


def test_finding_text_and_citation_quote_are_redacted(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    run_id = run_with(
        ctx,
        repo_project,
        [
            {
                "type": "finding",
                "payload": {
                    "id": "F1",
                    "text": f"credential leaked: {LEAK}",
                    "links": [{"path": "src/greeter.py", "quote": f"here: {LEAK}"}],
                },
            }
        ],
    )
    body = client.get(f"/api/v1/runs/{run_id}/context/findings").json()
    assert LEAK not in json.dumps(body)
    [entry] = [f for f in body if f["key"] == "F1"]
    assert "[REDACTED:" in entry["text"]
    [citation] = entry["citations"]
    assert "[REDACTED:" in (citation["quote"] or "")
    assert citation["details"]["quote_redacted"] is True


def test_direct_orm_inserts_are_redacted_by_the_guard(ctx: AppContext, repo_project: str) -> None:
    """Inserting Finding/Citation/CitationVerdict rows directly (bypassing FindingsService)
    still gets redacted: the before_flush guard covers the new columns."""
    run_id = run_with(ctx, repo_project, [], prompt="direct insert")
    with ctx.session() as session:
        # fixture only: production code writes these rows through FindingsService
        finding = Finding(
            run_id=run_id,
            attempt_id=None,
            node_key=None,
            origin_event_id=new_id(),
            sequence=0,
            finding_key="direct",
            kind="claim",
            text=f"leak {LEAK}",
            source="test",
            details={"note": f"leak {LEAK}"},
            created_at=ctx.clock.now(),
        )
        session.add(finding)
        session.flush()
        citation = Citation(
            finding_id=finding.id,
            position=0,
            kind="file",
            path="a.py",
            inside_boundary=True,
            line_start=1,
            line_end=1,
            quote=f"quote {LEAK}",
            event_ref=f"ref-{LEAK}",
            event_id=None,
            details={"note": f"leak {LEAK}"},
            created_at=ctx.clock.now(),
        )
        session.add(citation)
        session.flush()
        verdict = CitationVerdict(
            citation_id=citation.id,
            verdict="quote_matches",
            reason=f"because {LEAK}",
            method="test",
            verifier_version=None,
            evidence={"why": f"{LEAK}"},
            rerun_of_id=None,
            created_at=ctx.clock.now(),
        )
        session.add(verdict)
        session.flush()
        finding_id, citation_id, verdict_id = finding.id, citation.id, verdict.id

    with ctx.session() as session:
        stored_finding = session.get(Finding, finding_id)
        stored_citation = session.get(Citation, citation_id)
        stored_verdict = session.get(CitationVerdict, verdict_id)
        assert stored_finding is not None
        assert stored_citation is not None
        assert stored_verdict is not None
        blob = json.dumps(
            [
                stored_finding.text,
                stored_finding.details,
                stored_citation.quote,
                stored_citation.event_ref,
                stored_citation.details,
                stored_verdict.reason,
                stored_verdict.evidence,
            ]
        )
        assert LEAK not in blob
        assert "[REDACTED:" in (stored_finding.text or "")
        assert "[REDACTED:" in (stored_citation.quote or "")
        assert "[REDACTED:" in (stored_citation.event_ref or "")


def test_over_cap_citations_are_dropped_and_oversized_quote_is_truncated(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    big_quote = "y" * 5000
    links = [{"path": "big.py", "quote": big_quote}] + [{"path": f"f{i}.py"} for i in range(59)]
    run_id = run_with(
        ctx,
        repo_project,
        [{"type": "finding", "payload": {"id": "F1", "text": "many citations", "links": links}}],
    )
    body = client.get(f"/api/v1/runs/{run_id}/context/findings").json()
    [entry] = [f for f in body if f["key"] == "F1"]
    assert len(entry["citations"]) == 50
    assert entry["details"]["citations_dropped"] == 10
    big = next(c for c in entry["citations"] if c["path"] == "big.py")
    assert big["quote"] == "y" * 4000
    assert big["details"]["quote_truncated"] is True


def test_duplicate_finding_ids_in_one_run_get_disambiguated_keys(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    run_id = run_with(
        ctx,
        repo_project,
        [
            {"type": "finding", "payload": {"id": "F1", "text": "first"}},
            {"type": "finding", "payload": {"id": "F1", "text": "second"}},
        ],
    )
    body = client.get(f"/api/v1/runs/{run_id}/context/findings").json()
    keys = sorted(f["key"] for f in body)
    assert keys[0] == "F1"
    assert keys[1].startswith("F1@")


def test_agent_chosen_id_shaped_like_a_disambiguated_key_does_not_collide(
    ctx: AppContext, repo_project: str
) -> None:
    """A crafted/duplicate agent id shaped like "F1@<n>" must not collide with the key a
    same-run "F1" duplicate gets disambiguated to at sequence n: disambiguation uses the
    globally-unique origin event id, not the run-local sequence number."""
    run_id = run_with(ctx, repo_project, [], prompt="disambiguation collision")
    with ctx.session() as session:
        run = session.get(Run, run_id)
        assert run is not None

        def finding_event(sequence: int, finding_id: str, text: str) -> Event:
            return Event(
                id=new_id(),
                run_id=run_id,
                sequence=sequence,
                ts=ctx.clock.now(),
                type="finding",
                source="test",
                severity="info",
                summary=finding_id,
                payload={"id": finding_id, "text": text},
            )

        first_event = finding_event(9001, "F1", "first")
        session.add(first_event)
        session.flush()
        first_finding = FindingsService.ingest_event(session, run, first_event, None)
        session.flush()

        # Plants a landmine at the exact key the old sequence-based disambiguation would
        # produce for a duplicate "F1" landing at sequence 9002.
        crafted_event = finding_event(9000, "F1@9002", "crafted collision")
        session.add(crafted_event)
        session.flush()
        crafted_finding = FindingsService.ingest_event(session, run, crafted_event, None)
        session.flush()

        duplicate_event = finding_event(9002, "F1", "second")
        session.add(duplicate_event)
        session.flush()
        # Must not raise IntegrityError: the old `f"{finding_key}@{event.sequence}"`
        # disambiguation would collide with the crafted "F1@9002" row above.
        duplicate_finding = FindingsService.ingest_event(session, run, duplicate_event, None)
        session.flush()

        assert first_finding.finding_key == "F1"
        assert crafted_finding.finding_key == "F1@9002"
        assert duplicate_finding.finding_key == f"F1@{duplicate_event.id}"
        assert duplicate_finding.finding_key not in (
            first_finding.finding_key,
            crafted_finding.finding_key,
        )


def test_oversized_payload_is_stored_as_payload_truncated(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    huge_links = [{"path": f"f{i}.py", "quote": "z" * 2000} for i in range(60)]
    run_id = run_with(
        ctx,
        repo_project,
        [{"type": "finding", "payload": {"id": "F1", "text": "huge", "links": huge_links}}],
    )
    body = client.get(f"/api/v1/runs/{run_id}/context/findings").json()
    [entry] = body
    assert entry["details"]["payload_truncated"] is True
    assert entry["citations"] == []


def test_per_field_text_cap_also_sets_payload_truncated(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    """A finding `text` over the per-field cap (docs/EVENT-SCHEMA.md "Size caps") is
    truncated in place by the event sink (`text_truncated: true`), separately from the
    whole-payload cap. `payload_truncated` must catch this form too."""
    over_field_cap_text = "z" * (20 * 1024)  # over max_event_text_bytes (16 KiB)
    run_id = run_with(
        ctx,
        repo_project,
        [{"type": "finding", "payload": {"id": "F1", "text": over_field_cap_text}}],
    )
    body = client.get(f"/api/v1/runs/{run_id}/context/findings").json()
    [entry] = [f for f in body if f["key"] == "F1"]
    assert entry["details"]["payload_truncated"] is True


def test_record_verdict_chains_reruns_and_updates_check_count(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    run_id = run_with(
        ctx,
        repo_project,
        [
            {
                "type": "finding",
                "payload": {
                    "id": "F1",
                    "text": "claim",
                    "links": [{"path": "src/greeter.py", "quote": "def greet():"}],
                },
            }
        ],
    )
    body = client.get(f"/api/v1/runs/{run_id}/context/findings").json()
    [entry] = [f for f in body if f["key"] == "F1"]
    [citation] = entry["citations"]
    # The worker already verified this run's citations automatically once it finished.
    automatic_verdict = citation["verdict"]
    assert automatic_verdict is not None
    assert citation["checks"] == 1

    service = FindingsService(ctx)
    first = service.record_verdict(citation["id"], verdict="quote_matches", method="seeded")
    second = service.record_verdict(
        citation["id"], verdict="stale", method="seeded", reason="the file changed"
    )
    assert first["rerun_of_id"] == automatic_verdict["id"]
    assert second["rerun_of_id"] == first["id"]

    with ctx.session() as session:
        stored_first = session.get(CitationVerdict, first["id"])
        assert stored_first is not None
        assert stored_first.verdict == "quote_matches"
        assert stored_first.rerun_of_id == automatic_verdict["id"]

    body_after = client.get(f"/api/v1/runs/{run_id}/context/findings").json()
    [entry_after] = [f for f in body_after if f["key"] == "F1"]
    [citation_after] = entry_after["citations"]
    assert citation_after["checks"] == 3
    assert citation_after["verdict"]["id"] == second["id"]
    assert citation_after["verdict"]["verdict"] == "stale"


def test_record_verdict_rejects_invalid_verdict_method_and_unknown_citation(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    run_id = run_with(
        ctx,
        repo_project,
        [
            {
                "type": "finding",
                "payload": {
                    "id": "F1",
                    "text": "claim",
                    "links": [{"path": "src/greeter.py", "quote": "def greet():"}],
                },
            }
        ],
    )
    body = client.get(f"/api/v1/runs/{run_id}/context/findings").json()
    [entry] = [f for f in body if f["key"] == "F1"]
    [citation] = entry["citations"]
    service = FindingsService(ctx)

    with pytest.raises(ValidationError):
        service.record_verdict(citation["id"], verdict="declared_only", method="seeded")
    with pytest.raises(ValidationError):
        service.record_verdict(citation["id"], verdict="not-a-real-verdict", method="seeded")
    with pytest.raises(ValidationError):
        service.record_verdict(citation["id"], verdict="quote_matches", method="")
    with pytest.raises(ValidationError):
        service.record_verdict(citation["id"], verdict="quote_matches", method="x" * 33)
    with pytest.raises(NotFoundError):
        service.record_verdict("does-not-exist", verdict="quote_matches", method="seeded")


def test_deleting_run_deletes_findings_citations_and_verdicts(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    run_id = run_with(
        ctx,
        repo_project,
        [
            {
                "type": "finding",
                "payload": {
                    "id": "F1",
                    "text": "claim",
                    "links": [{"path": "src/greeter.py", "quote": "def greet():"}],
                },
            }
        ],
    )
    body = client.get(f"/api/v1/runs/{run_id}/context/findings").json()
    [entry] = [f for f in body if f["key"] == "F1"]
    [citation] = entry["citations"]
    finding_id, citation_id = entry["id"], citation["id"]
    FindingsService(ctx).record_verdict(citation_id, verdict="quote_matches", method="seeded")

    with ctx.session() as session:
        session.execute(delete(Run).where(Run.id == run_id))

    with ctx.session() as session:
        assert session.get(Finding, finding_id) is None
        assert session.get(Citation, citation_id) is None
        assert (
            session.scalars(
                select(CitationVerdict).where(CitationVerdict.citation_id == citation_id)
            ).first()
            is None
        )
