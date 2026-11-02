"""Context evidence unit tests over synthetic events of the contract's shape (M2)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from agenttape.application.context import AppContext
from agenttape.application.events import EventRecorder
from agenttape.context_evidence import service
from agenttape.context_evidence.common import safe_locator, sanitize
from agenttape.context_evidence.normalize import normalize_run
from agenttape.context_evidence.pack import assemble_pack
from agenttape.domain.enums import EventType
from agenttape.domain.errors import ConflictError, ValidationError
from agenttape.infrastructure.db.extra_models.context_evidence import (
    ContextEvidenceEvent,
    ContextFeedback,
    ContextSource,
    EvidenceLink,
)
from agenttape.infrastructure.db.models import AuditEntry, Run, RunAttempt
from tests.conftest import demo_profile, start_run


def src(
    sid: str, *, live: bool | None = True, authority: str = "authoritative", **extra: Any
) -> dict[str, Any]:
    return {
        "id": sid,
        "type": extra.pop("type", "knowledge_doc"),
        "name": extra.pop("name", sid),
        "provider": "fixture",
        "locator": extra.pop("locator", f"kb/{sid}"),
        "authority": authority,
        "version": extra.pop("version", None),
        "synced_at": extra.pop("synced_at", None),
        "live": live,
    }


def ctx_record(stage: str, source: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "stage": stage,
        "source": source,
        "query": extra.get("query"),
        "tool": extra.get("tool"),
        "result": extra.get("result"),
        "reason": extra.get("reason"),
    }


@pytest.fixture
def run_id(ctx: AppContext) -> str:
    return start_run(ctx, demo_profile(ctx))


def record(ctx: AppContext, run_id: str, etype: EventType, payload: dict[str, Any]) -> None:
    with ctx.session() as session:
        EventRecorder(ctx.settings, ctx.clock).record(
            session, run_id, etype, str(payload.get("stage") or etype.value), payload
        )


def normalize(ctx: AppContext, run_id: str, workdir: Path | None = None) -> None:
    with ctx.session() as session:
        normalize_run(session, ctx.clock, run_id, 0, workdir)


def rows(ctx: AppContext, run_id: str) -> list[ContextEvidenceEvent]:
    with ctx.session() as session:
        out = list(
            session.scalars(
                select(ContextEvidenceEvent)
                .where(ContextEvidenceEvent.run_id == run_id)
                .order_by(ContextEvidenceEvent.sequence, ContextEvidenceEvent.position)
            )
        )
        session.expunge_all()
        return out


def add_attempt(ctx: AppContext, run_id: str, number: int) -> str:
    with ctx.session() as session:
        attempt = RunAttempt(run_id=run_id, attempt_number=number, status="running")
        session.add(attempt)
        session.flush()
        return attempt.id


# --------------------------------------------------------------------------- stages


def test_every_stage_is_classified_as_reported(ctx: AppContext, run_id: str) -> None:
    stages = [
        "available",
        "selected",
        "retrieved",
        "injected",
        "referenced",
        "cited",
        "unavailable",
        "excluded",
        "conflict",
        "stale",
        "gap",
    ]
    for stage in stages:
        record(ctx, run_id, EventType.CONTEXT, ctx_record(stage, src(f"s-{stage}")))
    record(ctx, run_id, EventType.CONTEXT, ctx_record("pondered", src("s-weird")))
    normalize(ctx, run_id)
    got = {r.source_key: r.stage for r in rows(ctx, run_id)}
    for stage in stages:
        assert got[f"s-{stage}"] == stage
    # an unrecognised stage is never upgraded to a stronger claim
    assert got["s-weird"] == "unknown"
    # gap/unavailable/stale/conflict each open a pending review item, nothing else does
    with ctx.session() as session:
        kinds = sorted(
            session.scalars(select(ContextFeedback.kind).where(ContextFeedback.run_id == run_id))
        )
        assert kinds == ["conflict", "gap", "stale", "unavailable"]
        statuses = set(session.scalars(select(ContextFeedback.status)))
        assert statuses == {"pending"}


def test_freshness_and_authority_labels(ctx: AppContext, run_id: str) -> None:
    record(ctx, run_id, EventType.CONTEXT, ctx_record("retrieved", src("live-db", live=True)))
    record(
        ctx,
        run_id,
        EventType.CONTEXT,
        ctx_record(
            "retrieved",
            src(
                "kb-snap",
                live=False,
                authority="snapshot",
                synced_at="2026-09-01T00:00:00Z",
                version="abc123",
            ),
        ),
    )
    record(ctx, run_id, EventType.CONTEXT, ctx_record("stale", src("kb-old", live=False)))
    record(
        ctx,
        run_id,
        EventType.CONTEXT,
        ctx_record("retrieved", src("odd", authority="gospel", live=None)),
    )
    normalize(ctx, run_id)
    by_key = {r.source_key: r for r in rows(ctx, run_id)}
    assert by_key["live-db"].freshness == "live"
    assert by_key["kb-snap"].freshness == "snapshot"
    assert by_key["kb-snap"].synced_at == "2026-09-01T00:00:00Z"
    assert by_key["kb-snap"].version == "abc123"
    assert by_key["kb-snap"].authority == "snapshot"
    assert by_key["kb-old"].freshness == "stale"
    assert by_key["odd"].authority == "unknown"
    assert by_key["odd"].freshness == "unknown"
    with ctx.session() as session:
        summary = service.run_summary(session, run_id)
    assert summary["live_sources"] == 1
    assert summary["snapshot_sources"] == 2
    assert [w["source_key"] for w in summary["freshness_warnings"]] == ["kb-old"]


def test_dedup_keeps_every_access_and_is_idempotent(ctx: AppContext, run_id: str) -> None:
    for _ in range(3):
        record(ctx, run_id, EventType.FILE_READ, {"path": "src/app.py", "bytes": 10})
    normalize(ctx, run_id)
    normalize(ctx, run_id)  # a restart / retry re-scan never duplicates
    evidence = rows(ctx, run_id)
    assert [r.stage for r in evidence] == ["retrieved"] * 3
    assert {r.source_key for r in evidence} == {"file:src/app.py"}
    assert evidence[0].details["file_path"] == "src/app.py"
    with ctx.session() as session:
        assert session.scalar(select(func.count()).select_from(ContextSource)) == 1


def test_finding_links_group_by_relation(ctx: AppContext, run_id: str) -> None:
    record(
        ctx,
        run_id,
        EventType.CONTEXT,
        ctx_record(
            "retrieved", src("repo:greeter", type="repository_file", locator="src/greeter.py")
        ),
    )
    # the adapter's own read of the same file is the same source (R9)
    record(ctx, run_id, EventType.FILE_READ, {"path": "src/greeter.py", "bytes": 30})
    record(ctx, run_id, EventType.CONTEXT, ctx_record("retrieved", src("kb:style", live=False)))
    finding = {
        "id": "F1",
        "kind": "recommendation",
        "text": "Rename greet() to greeting()",
        "links": [
            {"source_id": "repo:greeter", "relation": "supports"},
            {"source_id": "kb:style", "relation": "conflicts"},
            {"source_id": "jira:ABC-1", "relation": "missing"},
        ],
    }
    record(ctx, run_id, EventType.FINDING, finding)
    normalize(ctx, run_id)
    normalize(ctx, run_id)
    with ctx.session() as session:
        assert session.scalar(select(func.count()).select_from(EvidenceLink)) == 3
        origins = set(session.scalars(select(EvidenceLink.origin)))
        assert origins == {"agent-declared"}
        [f] = service.findings(session, run_id)
    assert f["kind"] == "recommendation"
    assert [s["key"] for s in f["supports"]] == ["file:src/greeter.py"]
    assert [s["key"] for s in f["conflicts"]] == ["kb:style"]
    assert [s["key"] for s in f["missing"]] == ["jira:ABC-1"]
    stages = {(r.source_key, r.stage) for r in rows(ctx, run_id) if r.event_id}
    assert ("file:src/greeter.py", "cited") in stages
    assert ("file:src/greeter.py", "retrieved") in stages
    assert ("jira:ABC-1", "gap") in stages
    with ctx.session() as session:
        files = session.scalars(
            select(ContextSource).where(ContextSource.source_type == "repository_file")
        ).all()
        assert [s.source_key for s in files] == ["file:src/greeter.py"]
        cited = service.list_evidence(session, run_id, stage="retrieved")["items"]
    # the adapter-observed read is flagged as cited in this run
    assert {r["cited_in_run"] for r in cited if r["source_key"] == "file:src/greeter.py"} == {True}


def test_one_bad_event_never_blocks_the_rest(ctx: AppContext, run_id: str) -> None:
    record(ctx, run_id, EventType.CONTEXT, ctx_record("retrieved", src("a", version="v" * 500)))
    record(ctx, run_id, EventType.CONTEXT, ctx_record("retrieved", src("b")))
    normalize(ctx, run_id)
    by_key = {r.source_key: r for r in rows(ctx, run_id)}
    version = by_key["a"].version
    assert version is not None and len(version) == 200  # clipped to the column
    assert by_key["b"].stage == "retrieved"


def test_a_failing_event_is_recorded_as_unknown(
    ctx: AppContext, run_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agenttape.context_evidence import normalize as norm

    record(ctx, run_id, EventType.FINDING, {"id": "F", "text": "x", "links": []})
    record(ctx, run_id, EventType.CONTEXT, ctx_record("retrieved", src("after")))

    def boom(self: object, ev: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(norm._Normalizer, "finding", boom)
    normalize(ctx, run_id)
    got = {(r.source_key, r.stage) for r in rows(ctx, run_id)}
    assert (None, "unknown") in got and ("after", "retrieved") in got


def test_prompt_is_injected_only_when_the_agent_is_given_it(ctx: AppContext) -> None:
    from agenttape.context_evidence.pack import build_items

    delivered = start_run(ctx, demo_profile(ctx))
    scripted = start_run(ctx, demo_profile(ctx, "full_evidence"))
    with ctx.session() as session:
        stages = {}
        for rid in (delivered, scripted):
            run = session.get(Run, rid)
            assert run is not None
            builder, _ = build_items(session, run, None)
            stages[rid] = next(i for i in builder.items if i["kind"] == "prompt")
    assert stages[delivered]["stage"] == "injected"
    assert stages[scripted]["stage"] == "selected"
    assert "not given the prompt" in stages[scripted]["reason"]


def test_pack_hash_ignores_per_run_identifiers(ctx: AppContext) -> None:
    profile = demo_profile(ctx)
    a = start_run(ctx, profile, idempotency_key="seed:a")
    b = start_run(ctx, profile, idempotency_key="seed:b")
    hashes = []
    for rid in (a, b):
        attempt = add_attempt(ctx, rid, 1)
        with ctx.session() as session:
            hashes.append(assemble_pack(session, ctx.clock, rid, attempt, None).content_hash)
    assert hashes[0] == hashes[1]


def test_search_escapes_like_wildcards(ctx: AppContext, run_id: str) -> None:
    record(ctx, run_id, EventType.CONTEXT, ctx_record("retrieved", src("kb:plain")))
    record(ctx, run_id, EventType.CONTEXT, ctx_record("retrieved", src("kb:100%_sure")))
    normalize(ctx, run_id)
    with ctx.session() as session:
        hits = service.list_evidence(session, run_id, q="%_")["items"]
        assert [h["source_key"] for h in hits] == ["kb:100%_sure"]
        assert service.list_evidence(session, run_id, q="KB:PLAIN")["total"] == 1


def test_compare_keys_agent_declared_mcp_by_source(ctx: AppContext) -> None:
    profile = demo_profile(ctx)
    a, b = start_run(ctx, profile), start_run(ctx, profile)
    mcp = src("mcp:warehouse/run_query", type="mcp_tool")
    record(ctx, a, EventType.CONTEXT, ctx_record("available", mcp))
    record(ctx, b, EventType.CONTEXT, ctx_record("unavailable", mcp))
    normalize(ctx, a)
    normalize(ctx, b)
    with ctx.session() as session:
        cmp = service.compare(session, a, b)
    assert cmp["mcp"] == [{"tool": "mcp:warehouse/run_query", "a": "available", "b": "unavailable"}]


def test_token_and_cost_unavailable_unless_reported(ctx: AppContext, run_id: str) -> None:
    attempt = add_attempt(ctx, run_id, 1)
    with ctx.session() as session:
        pack = assemble_pack(session, ctx.clock, run_id, attempt, None)
        assert pack.token_estimate is None
        summary = service.run_summary(session, run_id)
    assert summary["usage"] == {"status": "unavailable", "values": {}}
    assert summary["pack"]["token_status"] == "unavailable"
    with ctx.session() as session:
        run = session.get(Run, run_id)
        assert run is not None
        run.usage = {"input_tokens": 10, "output_tokens": 5, "simulated": True}
    with ctx.session() as session:
        usage = service.run_summary(session, run_id)["usage"]
    assert usage["status"] == "simulated"
    assert usage["values"] == {"input_tokens": 10, "output_tokens": 5}


def test_pack_is_immutable_versioned_and_hash_is_stable(ctx: AppContext, run_id: str) -> None:
    a1 = add_attempt(ctx, run_id, 1)
    a2 = add_attempt(ctx, run_id, 2)
    with ctx.session() as session:
        first = assemble_pack(session, ctx.clock, run_id, a1, None)
        first_id, first_hash, first_items = first.id, first.content_hash, list(first.items)
    with ctx.session() as session:
        again = assemble_pack(session, ctx.clock, run_id, a1, None)  # resume: same pack
        assert (again.id, again.content_hash) == (first_id, first_hash)
        second = assemble_pack(session, ctx.clock, run_id, a2, None)
        assert second.version == 2
        # same inputs -> same content hash (ids and timestamps are not hashed)
        assert second.content_hash == first_hash
    kinds = [i["kind"] for i in first_items]
    assert kinds[:3] == ["trigger", "prompt", "agent_profile"]
    prompt = first_items[1]
    assert prompt["stage"] == "injected"  # AgentTape hands the rendered prompt to the agent
    assert all(i["stage"] == "selected" for i in first_items if i["kind"] != "prompt")
    # pack records are append-only evidence too
    assert {r.stage for r in rows(ctx, run_id)} >= {"selected", "injected", "excluded"}


def test_secret_redaction_and_sanitized_locators(tmp_path: Path) -> None:
    assert safe_locator("https://user:hunter2pass@db.example.com/x?token=abc") == (
        "https://db.example.com/x"
    )
    assert safe_locator("/etc/shadow") == "<outside>/shadow"
    assert safe_locator(str(tmp_path / "src" / "a.py"), tmp_path) == "src/a.py"
    assert safe_locator("~/.ssh/id_rsa", tmp_path) == "<outside>/id_rsa"
    pw = "hunter2" * 2
    clean = sanitize({"sql": "select 1", "password": pw, "note": "x" * 1000})
    assert clean["password"].startswith("[REDACTED")
    assert "hunter2hunter2" not in json.dumps(clean)
    assert len(clean["note"]) < 400


def test_mcp_tool_result_records_metadata_not_content(ctx: AppContext, run_id: str) -> None:
    leak = "-".join(["k", "1234567890abcdef"])  # fake, built at runtime
    with ctx.session() as session:
        rec = EventRecorder(ctx.settings, ctx.clock)
        call = rec.record(
            session,
            run_id,
            EventType.TOOL_CALL,
            "mcp",
            {
                "tool": "mcp__warehouse__run_query",
                "input": {"sql": "select id, total from orders", "api_key": leak},
                "tool_use_id": "t1",
            },
        )
        rec.record(
            session,
            run_id,
            EventType.TOOL_RESULT,
            "rows",
            {
                "tool_use_id": "t1",
                "output": json.dumps([{"id": 1, "total": 9}, {"id": 2, "total": 7}]),
                "is_error": False,
                "truncated": True,
            },
            parent_event_id=call.id,
        )
    normalize(ctx, run_id)
    [row] = rows(ctx, run_id)
    assert row.stage == "retrieved"
    assert row.source_type == "mcp_tool"
    assert row.tool is not None and row.result is not None
    assert row.tool["server"] == "warehouse" and row.tool["name"] == "run_query"
    assert row.tool["status"] == "ok"
    assert row.tool["args"]["sql"] == "select id, total from orders"
    assert leak not in json.dumps(row.tool)
    assert row.result["rows"] == 2
    assert row.result["schema"] == ["id", "total"]
    assert row.result["truncated"] is True
    assert len(row.result["hash"]) == 64
    assert "total" not in json.dumps(row.details)  # no result content is stored


def test_read_with_offset_and_limit_records_requested_and_returned_range(
    ctx: AppContext, run_id: str
) -> None:
    output = "\n".join(f"{i:6d}\tline {i}" for i in range(10, 15))
    with ctx.session() as session:
        rec = EventRecorder(ctx.settings, ctx.clock)
        call = rec.record(
            session,
            run_id,
            EventType.TOOL_CALL,
            "read",
            {"tool": "Read", "input": {"file_path": "src/a.py", "offset": 10, "limit": 5}},
        )
        rec.record(
            session,
            run_id,
            EventType.TOOL_RESULT,
            "read-result",
            {"output": output, "is_error": False},
            parent_event_id=call.id,
        )
    normalize(ctx, run_id)
    [row] = rows(ctx, run_id)
    assert row.source_key == "file:src/a.py"
    assert row.details["requested"] == {"offset": 10, "limit": 5}
    assert row.details["whole_file"] is False
    assert row.details["returned_lines"] == {"start": 10, "end": 14}


def test_read_without_offset_or_limit_is_whole_file(ctx: AppContext, run_id: str) -> None:
    output = "\n".join(f"{i:6d}\tline {i}" for i in range(1, 4))
    with ctx.session() as session:
        rec = EventRecorder(ctx.settings, ctx.clock)
        call = rec.record(
            session,
            run_id,
            EventType.TOOL_CALL,
            "read",
            {"tool": "Read", "input": {"file_path": "src/b.py"}},
        )
        rec.record(
            session,
            run_id,
            EventType.TOOL_RESULT,
            "read-result",
            {"output": output, "is_error": False},
            parent_event_id=call.id,
        )
    normalize(ctx, run_id)
    [row] = rows(ctx, run_id)
    assert row.details["requested"] == {}
    assert row.details["whole_file"] is True
    assert row.details["returned_lines"] == {"start": 1, "end": 3}


def test_read_truncated_output_records_returned_truncated_and_last_numbered_line(
    ctx: AppContext, run_id: str
) -> None:
    output = "\n".join(f"{i:6d}\tline {i}" for i in range(1, 6))
    with ctx.session() as session:
        rec = EventRecorder(ctx.settings, ctx.clock)
        call = rec.record(
            session,
            run_id,
            EventType.TOOL_CALL,
            "read",
            {"tool": "Read", "input": {"file_path": "src/c.py"}},
        )
        rec.record(
            session,
            run_id,
            EventType.TOOL_RESULT,
            "read-result",
            {"output": output, "is_error": False, "output_truncated": True},
            parent_event_id=call.id,
        )
    normalize(ctx, run_id)
    [row] = rows(ctx, run_id)
    assert row.details["returned_truncated"] is True
    assert row.details["returned_lines"] == {"start": 1, "end": 5}


def test_read_with_non_numbered_output_records_requested_but_no_returned_lines(
    ctx: AppContext, run_id: str
) -> None:
    with ctx.session() as session:
        rec = EventRecorder(ctx.settings, ctx.clock)
        call = rec.record(
            session,
            run_id,
            EventType.TOOL_CALL,
            "read",
            {"tool": "Read", "input": {"file_path": "src/d.py", "limit": 5}},
        )
        rec.record(
            session,
            run_id,
            EventType.TOOL_RESULT,
            "read-result",
            {"output": "not the cat -n layout", "is_error": False},
            parent_event_id=call.id,
        )
    normalize(ctx, run_id)
    [row] = rows(ctx, run_id)
    assert "returned_lines" not in row.details
    assert row.details["requested"] == {"limit": 5}

    with ctx.session() as session:
        rec = EventRecorder(ctx.settings, ctx.clock)
        call2 = rec.record(
            session,
            run_id,
            EventType.TOOL_CALL,
            "read",
            {"tool": "Read", "input": {"file_path": "src/e.py"}},
        )
        rec.record(
            session,
            run_id,
            EventType.TOOL_RESULT,
            "read-result",
            {"output": "     1\thas a prefix but is an error", "is_error": True},
            parent_event_id=call2.id,
        )
    normalize(ctx, run_id)
    by_key = {r.source_key: r for r in rows(ctx, run_id)}
    assert "returned_lines" not in by_key["file:src/e.py"].details


def test_bash_sed_read_creates_one_retrieved_record_with_shell_read_details(
    ctx: AppContext, run_id: str
) -> None:
    with ctx.session() as session:
        rec = EventRecorder(ctx.settings, ctx.clock)
        call = rec.record(
            session,
            run_id,
            EventType.TOOL_CALL,
            "bash",
            {"tool": "Bash", "input": {"command": "sed -n '2,3p' src/greeter.py"}},
        )
        rec.record(
            session,
            run_id,
            EventType.TOOL_RESULT,
            "bash-result",
            {"output": "line2\nline3", "is_error": False},
            parent_event_id=call.id,
        )
    normalize(ctx, run_id)
    [row] = rows(ctx, run_id)
    assert row.source_key == "file:src/greeter.py"
    assert row.stage == "retrieved"
    assert row.details["shell_read"] == {"program": "sed", "line_start": 2, "line_end": 3}
    assert row.details["returned_lines"] == {"start": 2, "end": 3}


def test_bash_head_records_returned_lines_tail_from_end_does_not(
    ctx: AppContext, run_id: str
) -> None:
    with ctx.session() as session:
        rec = EventRecorder(ctx.settings, ctx.clock)
        call = rec.record(
            session,
            run_id,
            EventType.TOOL_CALL,
            "bash",
            {"tool": "Bash", "input": {"command": "head -n 50 src/greeter.py"}},
        )
        rec.record(
            session,
            run_id,
            EventType.TOOL_RESULT,
            "bash-result",
            {"output": "line1\nline2", "is_error": False},
            parent_event_id=call.id,
        )
        call2 = rec.record(
            session,
            run_id,
            EventType.TOOL_CALL,
            "bash",
            {"tool": "Bash", "input": {"command": "tail -n 5 x"}},
        )
        rec.record(
            session,
            run_id,
            EventType.TOOL_RESULT,
            "bash-result",
            {"output": "a\nb\nc", "is_error": False},
            parent_event_id=call2.id,
        )
    normalize(ctx, run_id)
    by_key = {r.source_key: r for r in rows(ctx, run_id)}
    assert by_key["file:src/greeter.py"].details["returned_lines"] == {"start": 1, "end": 2}
    assert "returned_lines" not in by_key["file:x"].details


def test_bash_cat_multiple_paths_creates_distinct_records_with_no_returned_lines(
    ctx: AppContext, run_id: str
) -> None:
    with ctx.session() as session:
        rec = EventRecorder(ctx.settings, ctx.clock)
        call = rec.record(
            session,
            run_id,
            EventType.TOOL_CALL,
            "bash",
            {"tool": "Bash", "input": {"command": "cat a.py b.py"}},
        )
        rec.record(
            session,
            run_id,
            EventType.TOOL_RESULT,
            "bash-result",
            {"output": "content a\ncontent b", "is_error": False},
            parent_event_id=call.id,
        )
    normalize(ctx, run_id)
    got = rows(ctx, run_id)
    assert {r.source_key for r in got} == {"file:a.py", "file:b.py"}
    assert len({r.dedupe_key for r in got}) == 2
    assert all("returned_lines" not in r.details for r in got)


def test_bash_piped_command_creates_no_record(ctx: AppContext, run_id: str) -> None:
    with ctx.session() as session:
        rec = EventRecorder(ctx.settings, ctx.clock)
        call = rec.record(
            session,
            run_id,
            EventType.TOOL_CALL,
            "bash",
            {"tool": "Bash", "input": {"command": "cat src/greeter.py | head"}},
        )
        rec.record(
            session,
            run_id,
            EventType.TOOL_RESULT,
            "bash-result",
            {"output": "irrelevant", "is_error": False},
            parent_event_id=call.id,
        )
    normalize(ctx, run_id)
    assert rows(ctx, run_id) == []


def test_bash_relative_path_resolves_against_the_calls_cwd(
    ctx: AppContext, run_id: str, tmp_path: Path
) -> None:
    with ctx.session() as session:
        rec = EventRecorder(ctx.settings, ctx.clock)
        call = rec.record(
            session,
            run_id,
            EventType.TOOL_CALL,
            "bash",
            {
                "tool": "Bash",
                "input": {"command": "cat greeter.py"},
                "cwd": str(tmp_path / "src"),
            },
        )
        rec.record(
            session,
            run_id,
            EventType.TOOL_RESULT,
            "bash-result",
            {"output": "content", "is_error": False},
            parent_event_id=call.id,
        )
    normalize(ctx, run_id, workdir=tmp_path)
    [row] = rows(ctx, run_id)
    assert row.source_key == "file:src/greeter.py"


def test_bash_absolute_path_outside_workdir_is_marked_outside(
    ctx: AppContext, run_id: str, tmp_path: Path
) -> None:
    with ctx.session() as session:
        rec = EventRecorder(ctx.settings, ctx.clock)
        call = rec.record(
            session,
            run_id,
            EventType.TOOL_CALL,
            "bash",
            {"tool": "Bash", "input": {"command": "cat /etc/passwd"}},
        )
        rec.record(
            session,
            run_id,
            EventType.TOOL_RESULT,
            "bash-result",
            {"output": "root:...", "is_error": False},
            parent_event_id=call.id,
        )
    normalize(ctx, run_id, workdir=tmp_path)
    [row] = rows(ctx, run_id)
    assert row.source_key == "file:<outside>/passwd"


def test_bash_and_read_events_are_idempotent_on_renormalize(ctx: AppContext, run_id: str) -> None:
    with ctx.session() as session:
        rec = EventRecorder(ctx.settings, ctx.clock)
        call = rec.record(
            session,
            run_id,
            EventType.TOOL_CALL,
            "bash",
            {"tool": "Bash", "input": {"command": "cat a.py b.py"}},
        )
        rec.record(
            session,
            run_id,
            EventType.TOOL_RESULT,
            "bash-result",
            {"output": "x\ny", "is_error": False},
            parent_event_id=call.id,
        )
    normalize(ctx, run_id)
    normalize(ctx, run_id)
    assert len(rows(ctx, run_id)) == 2


def test_summary_rebuild_is_deterministic(ctx: AppContext, run_id: str) -> None:
    record(ctx, run_id, EventType.CONTEXT, ctx_record("retrieved", src("a")))
    record(ctx, run_id, EventType.CONTEXT, ctx_record("gap", src("b"), reason="No access to b"))
    record(
        ctx,
        run_id,
        EventType.FINDING,
        {"id": "F", "text": "x", "links": [{"source_id": "a", "relation": "supports"}]},
    )
    normalize(ctx, run_id)
    with ctx.session() as session:
        first = service.run_summary(session, run_id)
        first_list = service.list_evidence(session, run_id)
    normalize(ctx, run_id)
    with ctx.session() as session:
        assert service.run_summary(session, run_id) == first
        assert service.list_evidence(session, run_id) == first_list


def test_runtime_reconnect_marks_the_window_incomplete(ctx: AppContext, run_id: str) -> None:
    record(
        ctx,
        run_id,
        EventType.RUNTIME,
        {"runtime": "herdr", "action": "reconnect", "offline_from": "t0", "offline_to": "t1"},
    )
    with ctx.session() as session:
        window = service.run_summary(session, run_id)["window"]
    assert window == {"complete": False, "offline": [{"offline_from": "t0", "offline_to": "t1"}]}


# --------------------------------------------------------------------------- learning loop


def test_gap_needs_classification_and_human_approval(ctx: AppContext, run_id: str) -> None:
    record(
        ctx,
        run_id,
        EventType.CONTEXT,
        {"stage": "correction", "text": "Use UTC", "category": "business-knowledge"},
    )
    normalize(ctx, run_id)
    with ctx.session() as session:
        [fb] = service.list_feedback(session, run_id=run_id)
    assert fb["status"] == "pending"
    assert fb["suggested_classification"] == "business-knowledge"
    assert fb["classification"] is None  # a suggestion is never applied automatically
    with ctx.session() as session, pytest.raises(ValidationError):
        service.approve(session, ctx.clock, fb["id"], actor="me")
    with ctx.session() as session:
        out = service.classify(session, ctx.clock, fb["id"], "agent-behavior", actor="me")
    assert out["proposed_destination"] == "prompt_revision"
    with ctx.session() as session:
        out = service.propose(
            session, ctx.clock, fb["id"], "no_promotion", "Use UTC everywhere", actor="me"
        )
    with ctx.session() as session:
        out = service.approve(session, ctx.clock, fb["id"], actor="reviewer")
    # no_promotion is approved as a decision but never becomes an injected lesson
    assert out["status"] == "approved" and out["lesson_version"] is None
    with ctx.session() as session, pytest.raises(ConflictError):
        service.reject(session, ctx.clock, fb["id"], actor="me")
    with ctx.session() as session:
        actions = sorted(
            session.scalars(select(AuditEntry.action).where(AuditEntry.resource_id == fb["id"]))
        )
    assert actions == [
        "context_feedback.approve",
        "context_feedback.classify",
        "context_feedback.propose",
    ]


def test_lesson_without_project_cannot_be_injected(ctx: AppContext, run_id: str) -> None:
    with ctx.session() as session:
        fb = service.create_feedback(
            session,
            ctx.clock,
            run_id,
            kind="lesson",
            text="Always run tests",
            source_key=None,
            actor="me",
        )
        service.classify(session, ctx.clock, fb["id"], "business-knowledge", actor="me")
    with ctx.session() as session, pytest.raises(ValidationError):
        service.approve(session, ctx.clock, fb["id"], actor="me")
