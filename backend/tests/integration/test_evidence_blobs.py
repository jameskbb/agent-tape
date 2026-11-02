"""Retention: every read a verdict is computed from is retained once, per read,
content-addressed, and dies with its run (Task 1). Task 2 adds Git-blob references, the
size cap, redaction, and pruned blobs as ``not_checkable``.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from agenttape.application.context import AppContext
from agenttape.application.schemas.git import RetentionExecute, RetentionSettings
from agenttape.application.services.retention import RetentionService
from agenttape.findings import retention as retention_module
from agenttape.infrastructure.db.extra_models.git_workspaces import RunWorkspace
from agenttape.infrastructure.db.extra_models.verification import EvidenceBlob, EvidenceRead
from agenttape.infrastructure.db.models import Run
from tests.integration.helpers import (
    GREETER_TEXT,
    citation_of,
    finding_record,
    read_records,
)
from tests.integration.test_context_evidence import LEAK, git, run_with

NOTES_TEXT = "line one\nline two\nline three\n"


def _evidence_reads(ctx: AppContext, run_id: str) -> list[EvidenceRead]:
    with ctx.session() as session:
        rows = list(session.scalars(select(EvidenceRead).where(EvidenceRead.run_id == run_id)))
        session.expunge_all()
        return rows


def _evidence_blobs(ctx: AppContext, run_id: str) -> list[EvidenceBlob]:
    with ctx.session() as session:
        rows = list(session.scalars(select(EvidenceBlob).where(EvidenceBlob.run_id == run_id)))
        session.expunge_all()
        return rows


def test_verification_retains_the_read_it_used(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    records = [
        *read_records("toolu_notes", "src/notes.py", NOTES_TEXT),
        finding_record("F1", "src/notes.py", 2, 2, "line two"),
    ]
    run_id = run_with(ctx, repo_project, records)

    resp = client.post(f"/api/v1/runs/{run_id}/findings/verify")
    assert resp.status_code == 200, resp.text

    reads = _evidence_reads(ctx, run_id)
    assert len(reads) == 1
    read = reads[0]
    assert read.path == "src/notes.py"
    assert read.line_first == 1
    assert read.line_last == 3

    blobs = _evidence_blobs(ctx, run_id)
    assert len(blobs) == 1
    blob = blobs[0]
    assert blob.storage_path is not None
    assert blob.storage_path.startswith(f"runs/{run_id}/evidence/")
    assert blob.git_blob is None
    assert ctx.artifact_store.resolve(blob.storage_path).is_file()

    citation = citation_of(client, run_id, "F1")
    assert citation["verdict"]["verdict"] == "quote_matches"
    assert citation["verdict"]["evidence"]["read"]["blob_ref"] == f"sha256:{blob.sha256}"

    resp2 = client.post(f"/api/v1/runs/{run_id}/findings/verify")
    assert resp2.status_code == 200, resp2.text
    assert len(_evidence_reads(ctx, run_id)) == 1
    assert len(_evidence_blobs(ctx, run_id)) == 1


def test_retained_evidence_is_deleted_with_the_run(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    records = [
        *read_records("toolu_notes2", "src/notes.py", NOTES_TEXT),
        finding_record("F1", "src/notes.py", 2, 2, "line two"),
    ]
    run_id = run_with(ctx, repo_project, records)
    resp = client.post(f"/api/v1/runs/{run_id}/findings/verify")
    assert resp.status_code == 200, resp.text

    blob = _evidence_blobs(ctx, run_id)[0]
    assert blob.storage_path is not None
    retained_path = ctx.artifact_store.resolve(blob.storage_path)
    assert retained_path.is_file()

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
    assert not _evidence_reads(ctx, run_id)
    assert not _evidence_blobs(ctx, run_id)
    assert not retained_path.exists()


def test_full_read_of_a_committed_file_is_a_git_reference(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    quote = "def greet():\n    return 'hi'"
    records = [
        *read_records("toolu_g1", "src/greeter.py", GREETER_TEXT),
        finding_record("F1", "src/greeter.py", 1, 2, quote),
    ]
    run_id = run_with(ctx, repo_project, records)

    resp = client.post(f"/api/v1/runs/{run_id}/findings/verify")
    assert resp.status_code == 200, resp.text

    read = _evidence_reads(ctx, run_id)[0]
    blob = _evidence_blobs(ctx, run_id)[0]
    assert blob.git_blob is not None
    assert blob.storage_path is None
    assert read.read_hash == blob.git_blob
    evidence_dir = ctx.artifact_store.resolve(f"runs/{run_id}/evidence")
    assert not evidence_dir.exists() or not any(evidence_dir.iterdir())

    citation = citation_of(client, run_id, "F1")
    assert citation["verdict"]["verdict"] == "quote_matches"
    assert citation["verdict"]["evidence"]["read"]["blob_ref"] == f"git:{blob.git_blob}"


def test_a_read_cut_by_its_own_limit_is_not_treated_as_the_whole_file(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    """``offset=0, limit=2`` on a longer file returns exactly the first two
    lines. Those lines happen to equal a committed blob (``src/greeter.py``), but the read
    was cut by its own limit, so it is kept as a local copy with no read hash - never a Git
    reference to another file's blob presented as this file's hash at read time."""
    longer = GREETER_TEXT + "\ndef other():\n    return 1\n"
    first_two = "".join(longer.splitlines(keepends=True)[:2])
    assert first_two == GREETER_TEXT
    call, result = read_records("toolu_limit", "src/longer.py", first_two)
    call["payload"]["input"].update({"offset": 0, "limit": 2})
    records = [call, result, finding_record("F1", "src/longer.py", 1, 1, "def greet():")]
    run_id = run_with(ctx, repo_project, records)

    resp = client.post(f"/api/v1/runs/{run_id}/findings/verify")
    assert resp.status_code == 200, resp.text

    [read] = _evidence_reads(ctx, run_id)
    [blob] = _evidence_blobs(ctx, run_id)
    assert blob.git_blob is None
    assert blob.storage_path is not None
    assert read.read_hash is None
    assert (read.line_first, read.line_last) == (1, 2)
    citation = citation_of(client, run_id, "F1")
    assert citation["verdict"]["verdict"] == "quote_matches"
    assert citation["verdict"]["evidence"]["read"]["blob_ref"] == f"sha256:{blob.sha256}"


def test_a_reloaded_git_reference_reports_redaction_truthfully(
    ctx: AppContext, client: TestClient, repo_project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reloading a Git-reference read reports ``redacted`` from whether its
    own redaction pass changed the text - False for clean content, True when a redaction
    rule (here, a stand-in for one added after the blob was stored) now masks part of it."""
    records = [
        *read_records("toolu_in01", "src/greeter.py", GREETER_TEXT),
        finding_record("F1", "src/greeter.py", 1, 1, "def greet():"),
    ]
    run_id = run_with(ctx, repo_project, records)
    resp = client.post(f"/api/v1/runs/{run_id}/findings/verify")
    assert resp.status_code == 200, resp.text
    [read] = _evidence_reads(ctx, run_id)
    [blob] = _evidence_blobs(ctx, run_id)
    assert blob.git_blob is not None

    with ctx.session() as session:
        clean = retention_module.load_read(ctx, session, session.get(EvidenceRead, read.id))
    assert clean.redacted is False
    assert "    return 'hi'" in [text for _, text in clean.numbered_lines]

    class _NewRule:
        @staticmethod
        def redact_text(text: str) -> str:
            return text.replace("'hi'", "[REDACTED:new_rule]")

    monkeypatch.setattr(retention_module, "DEFAULT_REDACTOR", _NewRule())
    with ctx.session() as session:
        masked = retention_module.load_read(ctx, session, session.get(EvidenceRead, read.id))
    assert masked.redacted is True
    assert "    return [REDACTED:new_rule]" in [text for _, text in masked.numbered_lines]


def test_redacted_content_is_stored_redacted(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    text = f"line one\n{LEAK}\nline three\n"
    records = [
        *read_records("toolu_leak", "src/secret.py", text),
        finding_record("F1", "src/secret.py", 1, 1, "line one"),
    ]
    run_id = run_with(ctx, repo_project, records)

    resp = client.post(f"/api/v1/runs/{run_id}/findings/verify")
    assert resp.status_code == 200, resp.text

    blob = _evidence_blobs(ctx, run_id)[0]
    assert blob.redacted is True
    assert blob.storage_path is not None
    data = ctx.artifact_store.resolve(blob.storage_path).read_bytes()
    assert b"[REDACTED:" in data
    assert LEAK.encode() not in data


def test_retained_content_is_size_capped(
    ctx: AppContext, client: TestClient, repo_project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("agenttape.findings.retained_output.MAX_RETAINED_BYTES", 200)
    lines_text = "\n".join(f"line {i}" for i in range(1, 51)) + "\n"
    records = [
        *read_records("toolu_cap", "src/big.py", lines_text),
        finding_record("F1", "src/big.py", 1, 1, "line 1"),
        finding_record("F2", "src/big.py", 40, 40, "line 40"),
    ]
    run_id = run_with(ctx, repo_project, records)

    resp = client.post(f"/api/v1/runs/{run_id}/findings/verify")
    assert resp.status_code == 200, resp.text

    read = _evidence_reads(ctx, run_id)[0]
    assert read.truncated is True

    c1 = citation_of(client, run_id, "F1")
    assert c1["verdict"]["verdict"] == "quote_matches"

    c2 = citation_of(client, run_id, "F2")
    assert c2["verdict"]["verdict"] == "not_checkable"
    assert c2["verdict"]["evidence"]["not_checkable_reason"] == "size_cap_exceeded"


def test_pruned_blob_is_not_checkable(
    ctx: AppContext, client: TestClient, repo_project: str, tmp_path: Path
) -> None:
    # A local copy whose retained file has been deleted.
    records = [
        *read_records("toolu_prune1", "src/notes.py", NOTES_TEXT),
        finding_record("F1", "src/notes.py", 2, 2, "line two"),
    ]
    run_id = run_with(ctx, repo_project, records)
    resp = client.post(f"/api/v1/runs/{run_id}/findings/verify")
    assert resp.status_code == 200, resp.text
    blob = _evidence_blobs(ctx, run_id)[0]
    assert blob.storage_path is not None
    ctx.artifact_store.resolve(blob.storage_path).unlink()

    resp2 = client.post(f"/api/v1/runs/{run_id}/findings/verify")
    assert resp2.status_code == 200, resp2.text
    citation = citation_of(client, run_id, "F1")
    assert citation["verdict"]["verdict"] == "not_checkable"
    assert citation["verdict"]["evidence"]["not_checkable_reason"] == "blob_unavailable"

    # A Git reference whose blob is no longer in the run's (now repointed) repository.
    quote = "def greet():\n    return 'hi'"
    records2 = [
        *read_records("toolu_g2", "src/greeter.py", GREETER_TEXT),
        finding_record("F2", "src/greeter.py", 1, 2, quote),
    ]
    run_id2 = run_with(ctx, repo_project, records2)
    resp3 = client.post(f"/api/v1/runs/{run_id2}/findings/verify")
    assert resp3.status_code == 200, resp3.text
    blob2 = _evidence_blobs(ctx, run_id2)[0]
    assert blob2.git_blob is not None

    empty_repo = tmp_path / "empty-repo"
    empty_repo.mkdir()
    git(empty_repo, "init", "--quiet", "--initial-branch=main")
    with ctx.session() as session:
        session.execute(
            update(RunWorkspace)
            .where(RunWorkspace.run_id == run_id2)
            .values(repo_path=str(empty_repo))
        )

    resp4 = client.post(f"/api/v1/runs/{run_id2}/findings/verify")
    assert resp4.status_code == 200, resp4.text
    citation2 = citation_of(client, run_id2, "F2")
    assert citation2["verdict"]["verdict"] == "not_checkable"
    assert citation2["verdict"]["evidence"]["not_checkable_reason"] == "blob_unavailable"
