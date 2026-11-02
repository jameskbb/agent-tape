"""A Read's output is kept for verification up to the retained-content cap, not the event cap.

Every event's text is capped at ``max_event_text_bytes`` (16 KiB), and
verification used to retain that already-capped ``tool_result`` output, so a whole-file
Read of a 26 KB file kept only 16 KiB and every citation past the cut became
``not_checkable``/``size_cap_exceeded``. The event row still stops at 16 KiB; the redacted
output of a read is also kept, content-addressed, up to ``MAX_RETAINED_BYTES`` (1 MiB), and
verification checks citations against that copy. Covered on both paths: Claude Code hooks
(an observed session) and a launched Claude Code run (stream-json through
``adapters/process.py``, whose lines can be far longer than the plain-output line limit).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from agenttape.application.context import AppContext
from agenttape.application.events import EventRecorder
from agenttape.application.schemas.catalog import AgentProfileCreate
from agenttape.application.schemas.git import RetentionExecute, RetentionSettings
from agenttape.application.services.profiles import AgentProfileService
from agenttape.application.services.retention import RetentionService
from agenttape.domain.enums import EventType
from agenttape.findings import retained_output
from agenttape.infrastructure.db.extra_models.verification import EvidenceBlob, EvidenceRead
from agenttape.infrastructure.db.models import Event, Run, RunAttempt
from agenttape.security import executables
from agenttape.worker.hooks import fire_run_terminal_sync
from tests.conftest import drive, start_run
from tests.integration.capture_helpers import (
    hooks_token,
    loopback_client,
    post_hook,
    post_read,
    post_tool_use,
    pre_read,
    session_end,
    session_start,
    svc_project,
)
from tests.integration.helpers import bash_tool_records, make_repo_project, read_records
from tests.integration.test_context_evidence import LEAK, run_with

EVENT_TEXT_CAP = 16 * 1024
SESSION_ID = "sess-long-read"


def numbered_file(count: int, *, leak_at: int | None = None) -> str:
    """``count`` distinct lines of about 50 bytes each; line ``leak_at`` holds a secret."""
    lines = []
    for n in range(1, count + 1):
        if n == leak_at:
            lines.append(f'API_TOKEN = "{LEAK}"')
        else:
            lines.append(f"line {n:06d}: the value of item {n} is {n * 7} units")
    return "\n".join(lines) + "\n"


def line_text(n: int) -> str:
    return f"line {n:06d}: the value of item {n} is {n * 7} units"


def finding_payload(key: str, path: str, line: int) -> dict[str, Any]:
    return {
        "id": key,
        "kind": "claim",
        "text": f"{path} line {line} says something",
        "links": [{"path": path, "line_start": line, "line_end": line, "quote": line_text(line)}],
    }


def record_findings(ctx: AppContext, run_id: str, findings: list[dict[str, Any]]) -> None:
    with ctx.session() as session:
        attempt_id = session.scalars(select(RunAttempt.id).where(RunAttempt.run_id == run_id)).one()
        for payload in findings:
            EventRecorder(ctx.settings, ctx.clock).record(
                session, run_id, EventType.FINDING, payload["id"], payload, attempt_id=attempt_id
            )


def verdicts(client: TestClient, run_id: str) -> dict[str, dict[str, Any]]:
    resp = client.get(f"/api/v1/runs/{run_id}/context/findings")
    assert resp.status_code == 200, resp.text
    out: dict[str, dict[str, Any]] = {}
    for entry in resp.json():
        [citation] = entry["citations"]
        out[entry["key"]] = citation["verdict"]
    return out


def read_result_event(ctx: AppContext, run_id: str) -> Event:
    with ctx.session() as session:
        events = [
            e
            for e in session.scalars(
                select(Event).where(Event.run_id == run_id, Event.type == "tool_result")
            )
            if "output" in (e.payload or {}) or "output_retained" in (e.payload or {})
        ]
        session.expunge_all()
    [event] = [e for e in events if (e.payload or {}).get("output_retained")]
    return event


def retained_bytes(ctx: AppContext, event: Event) -> bytes:
    ref = event.payload["output_retained"]
    return ctx.artifact_store.resolve(ref["storage_path"]).read_bytes()


def blob_bytes(ctx: AppContext, run_id: str) -> list[bytes]:
    with ctx.session() as session:
        blobs = list(session.scalars(select(EvidenceBlob).where(EvidenceBlob.run_id == run_id)))
        session.expunge_all()
    return [
        ctx.artifact_store.resolve(b.storage_path).read_bytes() for b in blobs if b.storage_path
    ]


# ------------------------------------------------------------------------ hook path


def observed_read(
    ctx: AppContext,
    text: str,
    findings: list[dict[str, Any]],
    *,
    before_end: Callable[[str], None] | None = None,
) -> tuple[TestClient, str]:
    """An observed Claude Code session that reads ``src/big.py`` (holding ``text``) whole,
    records ``findings``, and ends; returns the client and the run id (verified on close).
    ``before_end`` gets the run id just before the session ends."""
    _project_id, repo = svc_project(ctx)
    (repo / "src" / "big.py").write_text(text)
    client = loopback_client(ctx)
    token = hooks_token(ctx)
    file_path = str(repo / "src" / "big.py")
    cwd = str(repo)
    for body in (
        session_start(cwd, session_id=SESSION_ID),
        pre_read(cwd, file_path, session_id=SESSION_ID),
        post_read(cwd, file_path, session_id=SESSION_ID, content=text),
    ):
        assert post_hook(client, token, body).status_code == 200
    with ctx.session() as session:
        run_id = session.scalars(select(RunAttempt.run_id)).one()
    record_findings(ctx, run_id, findings)
    if before_end is not None:
        before_end(run_id)
    assert post_hook(client, token, session_end(cwd, session_id=SESSION_ID)).status_code == 200
    return client, run_id


def test_hook_read_past_the_event_cap_verifies_lines_near_its_end(ctx: AppContext) -> None:
    text = numbered_file(600)  # about 30 KB, 600 lines
    assert len(text.encode()) > EVENT_TEXT_CAP * 1.5
    client, run_id = observed_read(
        ctx,
        text,
        [
            finding_payload("F-early", "src/big.py", 10),
            finding_payload("F-late", "src/big.py", 590),
        ],
    )
    got = verdicts(client, run_id)
    assert got["F-early"]["verdict"] == "quote_matches"
    assert got["F-late"]["verdict"] == "quote_matches", got["F-late"]

    # The event row itself is still capped at the event text cap.
    event = read_result_event(ctx, run_id)
    assert event.payload["output_truncated"] is True
    assert len(event.payload["output"].encode()) <= EVENT_TEXT_CAP
    assert event.payload["output_retained"]["truncated"] is False


def test_hook_read_past_the_retained_cap_stays_not_checkable(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A hook body is capped at 1 MiB (T23), so a hook can never deliver a read over the
    # 1 MiB retained cap; lower the cap to exercise the same bound on this path.
    monkeypatch.setattr(retained_output, "MAX_RETAINED_BYTES", 24 * 1024)
    text = numbered_file(600)
    client, run_id = observed_read(
        ctx,
        text,
        [
            finding_payload("F-inside", "src/big.py", 380),  # past 16 KiB, inside 24 KiB
            finding_payload("F-past", "src/big.py", 590),
        ],
    )
    got = verdicts(client, run_id)
    assert got["F-inside"]["verdict"] == "quote_matches", got["F-inside"]
    assert got["F-past"]["verdict"] == "not_checkable"
    assert got["F-past"]["evidence"]["not_checkable_reason"] == "size_cap_exceeded"
    event = read_result_event(ctx, run_id)
    assert event.payload["output_retained"]["truncated"] is True


def test_hook_read_retained_copy_is_redacted(ctx: AppContext) -> None:
    text = numbered_file(600, leak_at=500)
    client, run_id = observed_read(ctx, text, [finding_payload("F-late", "src/big.py", 590)])
    assert verdicts(client, run_id)["F-late"]["verdict"] == "quote_matches"
    event = read_result_event(ctx, run_id)
    kept = retained_bytes(ctx, event)
    assert LEAK.encode() not in kept
    assert b"[REDACTED" in kept
    assert line_text(590).encode() in kept
    for data in blob_bytes(ctx, run_id):
        assert LEAK.encode() not in data


def test_the_recorders_retained_copy_is_deleted_with_the_run(ctx: AppContext) -> None:
    """The copy the recorder keeps for an over-cap read, cited or not, is removed when
    retention cleanup deletes the run."""
    client, run_id = observed_read(ctx, numbered_file(600), [])
    path = ctx.artifact_store.resolve(
        read_result_event(ctx, run_id).payload["output_retained"]["storage_path"]
    )
    assert path.is_file()
    with ctx.session() as session:
        session.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(finished_at=ctx.clock.now() - timedelta(days=3))
        )
    service = RetentionService(ctx)
    service.update_settings(RetentionSettings(max_age_days=1), actor="test")
    result = service.execute(RetentionExecute(run_ids=[run_id]), actor="test")
    assert result.deleted_runs == [run_id]
    assert client.get(f"/api/v1/runs/{run_id}").status_code == 404
    assert not path.exists()


def test_retention_counts_the_recorders_retained_copy(ctx: AppContext) -> None:
    """The retained copy has no ``Artifact`` row, but cleanup deletes it, so the
    preview's artifact bytes and the freed bytes the cleanup reports both include it."""
    _client, run_id = observed_read(ctx, numbered_file(600), [])
    path = ctx.artifact_store.resolve(
        read_result_event(ctx, run_id).payload["output_retained"]["storage_path"]
    )
    size = path.stat().st_size
    with ctx.session() as session:
        session.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(finished_at=ctx.clock.now() - timedelta(days=3))
        )
    service = RetentionService(ctx)
    service.update_settings(RetentionSettings(max_age_days=1), actor="test")
    [item] = [i for i in service.preview().runs if i.run_id == run_id]
    assert item.artifact_bytes >= size
    assert item.total_bytes >= size
    result = service.execute(RetentionExecute(run_ids=[run_id]), actor="test")
    assert result.deleted_runs == [run_id]
    assert result.freed_bytes >= size


def hide_retained_copy(ctx: AppContext, run_id: str) -> tuple[Path, Path]:
    """Move the run's retained read copy aside; returns (where it was, where it is now)."""
    path = ctx.artifact_store.resolve(
        read_result_event(ctx, run_id).payload["output_retained"]["storage_path"]
    )
    aside = path.with_name(path.name + ".aside")
    path.rename(aside)
    return path, aside


def evidence_reads(ctx: AppContext, run_id: str) -> int:
    with ctx.session() as session:
        return len(list(session.scalars(select(EvidenceRead).where(EvidenceRead.run_id == run_id))))


def test_an_unloadable_retained_copy_is_blob_unavailable_and_recovers_on_recheck(
    ctx: AppContext,
) -> None:
    """A retained copy that cannot be loaded is "Cannot check: unavailable", never a
    16 KiB fallback that blames the size cap; nothing is retained from the capped event
    text, so a re-check once the copy loads again checks against the whole read."""
    text = numbered_file(600)
    hidden: list[tuple[Path, Path]] = []
    client, run_id = observed_read(
        ctx,
        text,
        [finding_payload("F-late", "src/big.py", 590)],
        before_end=lambda rid: hidden.append(hide_retained_copy(ctx, rid)),
    )
    got = verdicts(client, run_id)["F-late"]
    assert got["verdict"] == "not_checkable", got
    assert got["evidence"]["not_checkable_reason"] == "blob_unavailable", got
    assert evidence_reads(ctx, run_id) == 0

    [(path, aside)] = hidden
    aside.rename(path)
    assert client.post(f"/api/v1/runs/{run_id}/findings/verify").status_code == 200
    assert verdicts(client, run_id)["F-late"]["verdict"] == "quote_matches"
    assert evidence_reads(ctx, run_id) == 1


def test_a_placeholder_read_whose_retained_copy_is_gone_is_blob_unavailable(
    ctx: AppContext,
) -> None:
    """A read whose event payload is a size-cap placeholder (no ``output``) and whose
    retained copy is gone was numbered content that is now missing, not a read with no
    line numbers."""

    def placeholder_and_hide(rid: str) -> None:
        event = read_result_event(ctx, rid)
        with ctx.session() as session:
            row = session.get(Event, event.id)
            assert row is not None
            row.payload = {
                "truncated": True,
                "original_bytes": 2_000_000,
                "preview": "{}",
                "output_retained": event.payload["output_retained"],
            }
        hide_retained_copy(ctx, rid)

    client, run_id = observed_read(
        ctx,
        numbered_file(600),
        [finding_payload("F-late", "src/big.py", 590)],
        before_end=placeholder_and_hide,
    )
    got = verdicts(client, run_id)["F-late"]
    assert got["verdict"] == "not_checkable", got
    assert got["evidence"]["not_checkable_reason"] == "blob_unavailable", got


# ------------------------------------------------------------------------ launched path


def stream_json_read(file_path: str, text: str) -> list[dict[str, Any]]:
    """The stream-json records of a Claude Code session that reads ``file_path`` whole:
    the tool_result carries the ``cat -n`` text, and ``tool_use_result`` repeats the file
    content (Claude Code's own shape), so one line is about twice the file's size."""
    lines = text.splitlines()
    numbered = "\n".join(f"{n}\t{line}" for n, line in enumerate(lines, start=1))
    session = "5b7e7c52-7d0f-4a4e-9e55-3a3f1f7d2c10"
    return [
        {
            "type": "system",
            "subtype": "init",
            "cwd": "/work",
            "session_id": session,
            "tools": ["Read"],
            "model": "claude-opus-5-5",
            "permissionMode": "dontAsk",
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "claude-opus-5-5",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_long_read",
                        "name": "Read",
                        "input": {"file_path": file_path},
                    }
                ],
            },
            "parent_tool_use_id": None,
            "session_id": session,
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"tool_use_id": "toolu_long_read", "type": "tool_result", "content": numbered}
                ],
            },
            "parent_tool_use_id": None,
            "session_id": session,
            "tool_use_result": {
                "type": "text",
                "file": {
                    "filePath": file_path,
                    "content": text,
                    "numLines": len(lines),
                    "startLine": 1,
                    "totalLines": len(lines),
                },
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "num_turns": 2,
            "result": "done",
            "session_id": session,
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "total_cost_usd": 0.001,
        },
    ]


def launched_read(ctx: AppContext, text: str, findings: list[dict[str, Any]]) -> tuple[str, int]:
    """A launched Claude Code run (a fake executable replaying stream-json) that reads
    ``src/big.py`` whole; ``findings`` are recorded after it and the run's terminal hooks
    fired again, so they are ingested and verified. Returns the run id and the length of
    the longest stream-json line."""
    project_id = make_repo_project(
        ctx, name="long-read-repo", project_name="LongRead", files={"src/big.py": text}
    )
    bin_dir = ctx.settings.data_dir.parent / "bin"
    bin_dir.mkdir(exist_ok=True)
    stream = bin_dir / "long-read.jsonl"
    records = [json.dumps(r) for r in stream_json_read("src/big.py", text)]
    stream.write_text("\n".join(records) + "\n", encoding="utf-8")
    exe = bin_dir / "claude"
    exe.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "sys.stdin.read()\n"
        f"for line in open({str(stream)!r}, encoding='utf-8'):\n"
        "    sys.stdout.write(line)\n"
        "    sys.stdout.flush()\n",
        encoding="utf-8",
    )
    exe.chmod(0o755)
    executables.save_entries(
        ctx.settings.data_dir, [*executables.stored_entries(ctx.settings.data_dir), str(exe)]
    )
    pid = (
        AgentProfileService(ctx)
        .create(
            AgentProfileCreate(
                name="long-read",
                adapter_type="claude-code",
                config={"executable": str(exe), "allowed_tools": ["Read"]},
            )
        )
        .id
    )
    run_id = start_run(ctx, pid, project_id=project_id)
    detail = drive(ctx, run_id, timeout=60.0)
    assert detail.run.status == "succeeded", detail.run.failure_message
    record_findings(ctx, run_id, findings)
    fire_run_terminal_sync(ctx, run_id, "succeeded")
    return run_id, max(len(r.encode()) for r in records)


def test_launched_read_past_the_event_cap_verifies_lines_near_its_end(
    ctx: AppContext, client: TestClient
) -> None:
    text = numbered_file(700, leak_at=500)  # about 35 KB
    run_id, longest = launched_read(
        ctx,
        text,
        [
            finding_payload("F-early", "src/big.py", 10),
            finding_payload("F-late", "src/big.py", 690),
        ],
    )
    # The Read's stream-json line is longer than the plain-output line limit.
    assert longest > 68 * 1024
    got = verdicts(client, run_id)
    assert got["F-early"]["verdict"] == "quote_matches", got["F-early"]
    assert got["F-late"]["verdict"] == "quote_matches", got["F-late"]

    event = read_result_event(ctx, run_id)
    assert event.payload["output_truncated"] is True
    assert len(event.payload["output"].encode()) <= EVENT_TEXT_CAP
    kept = retained_bytes(ctx, event)
    assert LEAK.encode() not in kept
    assert b"[REDACTED" in kept
    for data in blob_bytes(ctx, run_id):
        assert LEAK.encode() not in data


def test_launched_read_over_the_retained_cap_stays_not_checkable_past_it(
    ctx: AppContext, client: TestClient
) -> None:
    text = numbered_file(24_000)  # about 1.2 MB, over the 1 MiB retained cap
    assert len(text.encode()) > retained_output.MAX_RETAINED_BYTES
    run_id, _ = launched_read(
        ctx,
        text,
        [
            finding_payload("F-inside", "src/big.py", 15_000),
            finding_payload("F-past", "src/big.py", 23_990),
        ],
    )
    got = verdicts(client, run_id)
    assert got["F-inside"]["verdict"] == "quote_matches", got["F-inside"]
    assert got["F-past"]["verdict"] == "not_checkable"
    assert got["F-past"]["evidence"]["not_checkable_reason"] == "size_cap_exceeded"
    event = read_result_event(ctx, run_id)
    ref = event.payload["output_retained"]
    assert ref["truncated"] is True
    assert ref["bytes"] <= retained_output.MAX_RETAINED_BYTES
    assert len(retained_bytes(ctx, event)) == ref["bytes"]


# ------------------------------------------------------------------------ the reference


def test_only_the_recorder_names_a_retained_copy(ctx: AppContext, repo_project: str) -> None:
    """A tool_result an adapter reports with its own ``output_retained`` never keeps it: a
    small read gets none, and a large one gets the recorder's own reference to what it
    actually received, so a payload can never point verification at other content."""
    forged = {
        "storage_path": "runs/other/evidence/" + "0" * 64,
        "sha256": "0" * 64,
        "bytes": 10,
        "truncated": False,
    }
    small = read_records("toolu_small", "src/small.py", "one\ntwo\n")
    big = read_records("toolu_big", "src/big.py", numbered_file(600))
    for record in (small[1], big[1]):
        record["payload"]["output_retained"] = dict(forged)
    run_id = run_with(ctx, repo_project, [*small, *big])
    with ctx.session() as session:
        results = {
            e.payload["tool_use_id"]: e.payload
            for e in session.scalars(
                select(Event).where(Event.run_id == run_id, Event.type == "tool_result")
            )
        }
    assert "output_retained" not in results["toolu_small"]
    ref = results["toolu_big"]["output_retained"]
    assert ref["sha256"] != forged["sha256"]
    assert ref["storage_path"] == f"runs/{run_id}/evidence/{ref['sha256']}"
    assert ctx.artifact_store.resolve(ref["storage_path"]).is_file()


def test_a_non_read_tool_result_is_not_kept(ctx: AppContext, repo_project: str) -> None:
    """Only a read's output is kept beyond the event cap; a long Bash result that is not a
    shell read (here a test run) stays as the capped event alone."""
    records = bash_tool_records("toolu_tests", "pytest -q", output=numbered_file(600))
    run_id = run_with(ctx, repo_project, records)
    with ctx.session() as session:
        [payload] = [
            e.payload
            for e in session.scalars(
                select(Event).where(Event.run_id == run_id, Event.type == "tool_result")
            )
        ]
    assert payload["output_truncated"] is True
    assert "output_retained" not in payload


def test_a_hook_read_of_an_image_is_not_kept(ctx: AppContext) -> None:
    """A hook ``Read`` of an image or PDF is recorded as a sorted JSON dump
    that carries base64 content; it can never back a line citation, so nothing is written
    to disk for it however large it is."""
    import base64

    _project_id, repo = svc_project(ctx)
    client = loopback_client(ctx)
    token = hooks_token(ctx)
    cwd = str(repo)
    image_path = str(repo / "diagram.png")
    data = base64.b64encode(bytes(range(256)) * 120).decode()  # about 40 KB of base64
    response = {"type": "image", "file": {"base64": data, "type": "image/png"}}
    for body in (
        session_start(cwd, session_id=SESSION_ID),
        pre_read(cwd, image_path, session_id=SESSION_ID),
        post_tool_use(
            cwd,
            tool_name="Read",
            tool_use_id="toolu_R1",
            tool_input={"file_path": image_path},
            tool_response=response,
            session_id=SESSION_ID,
        ),
    ):
        assert post_hook(client, token, body).status_code == 200
    with ctx.session() as session:
        [payload] = [
            e.payload
            for e in session.scalars(select(Event).where(Event.type == "tool_result"))
            if e.payload
        ]
    assert payload.get("response_shape") == "json"
    assert "output_retained" not in payload


def test_a_read_tool_call_with_no_path_is_not_kept(ctx: AppContext, repo_project: str) -> None:
    """File Evidence records a tool read only when its input names a path, so
    a large result of a read tool call with no path is not kept beyond the event cap."""
    records = read_records("toolu_nopath", "src/big.py", numbered_file(600))
    records[0]["payload"]["input"] = {}
    run_id = run_with(ctx, repo_project, records)
    with ctx.session() as session:
        [payload] = [
            e.payload
            for e in session.scalars(
                select(Event).where(Event.run_id == run_id, Event.type == "tool_result")
            )
        ]
    assert payload["output_truncated"] is True
    assert "output_retained" not in payload


def test_an_event_citation_of_a_long_reads_result_uses_the_retained_copy(
    ctx: AppContext,
) -> None:
    """An event citation of a read's ``tool_result`` is checked against the
    same retained copy a file citation of that read is, not the 16 KiB event text, so the
    same quote gets the same answer either way; the verdict says which content it used."""
    event_finding = {
        "id": "F-event",
        "kind": "claim",
        "text": "the read's result says something near its end",
        "links": [{"event_ref": "toolu_R1", "quote": line_text(590)}],
    }
    client, run_id = observed_read(
        ctx,
        numbered_file(600),
        [finding_payload("F-file", "src/big.py", 590), event_finding],
    )
    got = verdicts(client, run_id)
    assert got["F-file"]["verdict"] == "quote_matches", got["F-file"]
    assert got["F-event"]["verdict"] == "quote_matches", got["F-event"]
    assert got["F-event"]["evidence"]["event"]["retained"] is True
    assert got["F-event"]["evidence"]["line_range"] == [590, 590]


@pytest.mark.parametrize("flag", ["truncated", "output_truncated"])
def test_an_adapters_own_truncation_flag_is_kept_on_the_retained_copy(
    ctx: AppContext, repo_project: str, flag: str
) -> None:
    """A read result an adapter reports as already cut upstream stays marked
    truncated on its retained copy, so verification never treats it as a possible whole
    file (a Git reference or a ``read_hash``), even though the 1 MiB cut did not apply."""
    records = read_records("toolu_cut", "src/big.py", numbered_file(600))
    records[1]["payload"][flag] = True
    run_id = run_with(ctx, repo_project, records)
    with ctx.session() as session:
        [payload] = [
            e.payload
            for e in session.scalars(
                select(Event).where(Event.run_id == run_id, Event.type == "tool_result")
            )
        ]
    assert payload["output_retained"]["truncated"] is True
