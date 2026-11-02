"""``GET /citations/{citation_id}/content``: the cited-line read
behind the Findings UI's ``CitationView``.

Every behavior here is proved against a real, finished run with the real verification
engine's own verdicts - never a hand-crafted verdict row - so a citation's content always
comes from what AgentTape actually retained, resolved only inside the citation's own run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from agenttape.application.context import AppContext
from agenttape.infrastructure.db.extra_models.git_workspaces import RunWorkspace
from agenttape.infrastructure.db.extra_models.verification import EvidenceBlob
from agenttape.infrastructure.db.models import Event
from tests.integration.helpers import (
    GREETER_TEXT,
    citation_of,
    command_record,
    file_write_record,
    finding_record,
    link_finding,
    read_records,
)
from tests.integration.test_context_evidence import LEAK, git, run_with

NOTES_TEN = "\n".join(f"line {i}" for i in range(1, 11)) + "\n"
NOTES_TEXT = "line one\nline two\nline three\n"


def _content(client: TestClient, citation_id: str, **params: Any) -> Any:
    return client.get(f"/api/v1/citations/{citation_id}/content", params=params)


def test_content_returns_retained_lines_for_quote_matches(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    records = [
        *read_records("toolu_ctx", "src/notes.py", NOTES_TEN),
        finding_record("F1", "src/notes.py", 5, 5, "line 5"),
    ]
    run_id = run_with(ctx, repo_project, records)
    resp = client.post(f"/api/v1/runs/{run_id}/findings/verify")
    assert resp.status_code == 200, resp.text
    citation = citation_of(client, run_id, "F1")
    assert citation["verdict"]["verdict"] == "quote_matches"

    resp2 = _content(client, citation["id"], context=1)
    assert resp2.status_code == 200, resp2.text
    body = resp2.json()
    assert body["resolved_by"] == "verdict_read"
    assert body["cited"] == [5, 5]
    assert [n for n, _ in body["lines"]] == [4, 5, 6]
    assert body["lines"][1] == [5, "line 5"]
    assert body["unavailable_reason"] is None
    with ctx.session() as session:
        read_event = session.get(Event, body["read_result_event_id"])
        assert read_event is not None and read_event.run_id == run_id


def test_content_404_for_unknown_citation(client: TestClient) -> None:
    resp = _content(client, "does-not-exist")
    assert resp.status_code == 404, resp.text


def test_content_refuses_event_citation(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    records = [
        command_record("cmd_1", ["python3", "-m", "compileall"], "2 checks passed"),
        link_finding("F1", [{"event_ref": "cmd_1", "quote": "2 checks passed"}]),
    ]
    run_id = run_with(ctx, repo_project, records)
    citation = citation_of(client, run_id, "F1")
    resp = _content(client, citation["id"])
    assert resp.status_code == 422, resp.text


def test_content_outside_boundary(ctx: AppContext, client: TestClient, repo_project: str) -> None:
    records = [link_finding("F1", [{"path": "../../etc/hosts", "quote": "root:x:0:0"}])]
    run_id = run_with(ctx, repo_project, records)
    citation = citation_of(client, run_id, "F1")
    assert citation["inside_boundary"] is False

    resp = _content(client, citation["id"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["unavailable_reason"] == "outside_boundary"
    assert body["lines"] == []


def test_content_resolves_covering_read_for_quote_not_found(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    records = [
        *read_records("toolu_g1", "src/greeter.py", GREETER_TEXT),
        finding_record("F1", "src/greeter.py", 1, 1, "def greet(name):"),
    ]
    run_id = run_with(ctx, repo_project, records)
    resp = client.post(f"/api/v1/runs/{run_id}/findings/verify")
    assert resp.status_code == 200, resp.text
    citation = citation_of(client, run_id, "F1")
    assert citation["verdict"]["verdict"] == "quote_not_found"

    resp2 = _content(client, citation["id"])
    assert resp2.status_code == 200, resp2.text
    body = resp2.json()
    assert body["resolved_by"] == "covering_read"
    assert body["cited"] == [1, 1]
    assert body["unavailable_reason"] is None
    assert [t for n, t in body["lines"] if n == 1] == ["def greet():"]


def test_content_stale_uses_the_read_time_content(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    records = [
        *read_records("toolu_r1", "src/greeter.py", GREETER_TEXT),
        file_write_record("src/greeter.py", "modify", 40),
        finding_record("F1", "src/greeter.py", 1, 2, "def greet():\n    return 'hi'"),
    ]
    run_id = run_with(ctx, repo_project, records)
    resp = client.post(f"/api/v1/runs/{run_id}/findings/verify")
    assert resp.status_code == 200, resp.text
    citation = citation_of(client, run_id, "F1")
    assert citation["verdict"]["verdict"] == "stale"

    resp2 = _content(client, citation["id"])
    assert resp2.status_code == 200, resp2.text
    body = resp2.json()
    assert body["resolved_by"] == "verdict_read"
    assert [t for _, t in body["lines"]] == ["def greet():", "    return 'hi'"]


def test_content_never_crosses_into_another_runs_read(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    run_a = run_with(ctx, repo_project, read_records("toolu_a1", "src/greeter.py", GREETER_TEXT))
    run_b = run_with(
        ctx,
        repo_project,
        [finding_record("F1", "src/greeter.py", 1, 2, "def greet():\n    return 'hi'")],
    )
    assert run_a != run_b
    resp = client.post(f"/api/v1/runs/{run_b}/findings/verify")
    assert resp.status_code == 200, resp.text
    citation = citation_of(client, run_b, "F1")
    assert citation["verdict"] is None or citation["verdict"]["verdict"] != "quote_matches"

    resp2 = _content(client, citation["id"])
    assert resp2.status_code == 200, resp2.text
    body = resp2.json()
    assert body["unavailable_reason"] == "no_retained_read"
    assert body["lines"] == []


def test_content_pruned_blob_is_unavailable_never_500(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    records = [
        *read_records("toolu_notes", "src/notes.py", NOTES_TEXT),
        finding_record("F1", "src/notes.py", 2, 2, "line two"),
    ]
    run_id = run_with(ctx, repo_project, records)
    resp = client.post(f"/api/v1/runs/{run_id}/findings/verify")
    assert resp.status_code == 200, resp.text
    citation = citation_of(client, run_id, "F1")

    with ctx.session() as session:
        blob = session.scalars(select(EvidenceBlob).where(EvidenceBlob.run_id == run_id)).one()
        storage_path = blob.storage_path
        sha256 = blob.sha256
    assert storage_path is not None
    ctx.artifact_store.resolve(storage_path).unlink()

    resp2 = _content(client, citation["id"])
    assert resp2.status_code == 200, resp2.text
    body = resp2.json()
    assert body["unavailable_reason"] == "blob_unavailable"
    assert body["blob_ref"] == f"sha256:{sha256}"
    assert body["lines"] == []


def test_content_unavailable_reason_never_names_the_host_repository_path(
    ctx: AppContext, client: TestClient, repo_project: str, tmp_path: Path
) -> None:
    """A Git-anchored blob whose object is no longer in the run's (now
    repointed) repository answers a closed reason code, never the host filesystem path the
    old detail sentence embedded ("Git object <sha> is no longer in <repo path>")."""
    quote = "def greet():\n    return 'hi'"
    records = [
        *read_records("toolu_g2", "src/greeter.py", GREETER_TEXT),
        finding_record("F1", "src/greeter.py", 1, 2, quote),
    ]
    run_id = run_with(ctx, repo_project, records)
    resp = client.post(f"/api/v1/runs/{run_id}/findings/verify")
    assert resp.status_code == 200, resp.text
    citation = citation_of(client, run_id, "F1")

    with ctx.session() as session:
        blob = session.scalars(select(EvidenceBlob).where(EvidenceBlob.run_id == run_id)).one()
        assert blob.git_blob is not None
        repo_path = session.scalars(
            select(RunWorkspace.repo_path).where(RunWorkspace.run_id == run_id)
        ).one()

    empty_repo = tmp_path / "empty-repo"
    empty_repo.mkdir()
    git(empty_repo, "init", "--quiet", "--initial-branch=main")
    with ctx.session() as session:
        session.execute(
            update(RunWorkspace)
            .where(RunWorkspace.run_id == run_id)
            .values(repo_path=str(empty_repo))
        )

    resp2 = _content(client, citation["id"])
    assert resp2.status_code == 200, resp2.text
    assert repo_path not in resp2.text
    assert str(empty_repo) not in resp2.text
    body = resp2.json()
    assert body["unavailable_reason"] == "blob_unavailable"
    assert body["lines"] == []


@pytest.mark.parametrize("params", [{"context": 201}, {"before": -1}, {"after": 500}])
def test_content_bounds_are_422(client: TestClient, params: dict[str, int]) -> None:
    resp = _content(client, "any-citation-id", **params)
    assert resp.status_code == 422, resp.text


def test_content_context_zero_returns_only_cited_rows(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    records = [
        *read_records("toolu_ctx0", "src/notes.py", NOTES_TEN),
        finding_record("F1", "src/notes.py", 5, 5, "line 5"),
    ]
    run_id = run_with(ctx, repo_project, records)
    resp = client.post(f"/api/v1/runs/{run_id}/findings/verify")
    assert resp.status_code == 200, resp.text
    citation = citation_of(client, run_id, "F1")

    resp2 = _content(client, citation["id"], context=0)
    assert resp2.status_code == 200, resp2.text
    body = resp2.json()
    assert [n for n, _ in body["lines"]] == [5]


def test_content_clips_a_long_claimed_range(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    big = "\n".join(f"line {i}" for i in range(1, 1001)) + "\n"
    records = [
        *read_records("toolu_big2", "src/big2.py", big),
        finding_record("F1", "src/big2.py", 1, 900, "this text is not in the file"),
    ]
    run_id = run_with(ctx, repo_project, records)
    resp = client.post(f"/api/v1/runs/{run_id}/findings/verify")
    assert resp.status_code == 200, resp.text
    citation = citation_of(client, run_id, "F1")
    assert citation["verdict"]["verdict"] == "quote_not_found"

    resp2 = _content(client, citation["id"])
    assert resp2.status_code == 200, resp2.text
    body = resp2.json()
    assert body["clipped"] is True
    assert len(body["lines"]) == 800
    assert body["lines"][0][0] == 1
    assert body["lines"][-1][0] == 800


def test_content_redacts_secrets_even_when_retention_missed_them(
    ctx: AppContext,
    client: TestClient,
    repo_project: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defense in depth: the content endpoint re-redacts on the way out, so a
    secret that somehow reached retained storage unredacted still never appears in a
    response."""

    class _NoOpRedactor:
        @staticmethod
        def redact_text(text: str) -> str:
            return text

    monkeypatch.setattr("agenttape.findings.retention.DEFAULT_REDACTOR", _NoOpRedactor())
    text = f"line one\n{LEAK}\nline three\n"
    records = [
        *read_records("toolu_leak2", "src/secret2.py", text),
        finding_record("F1", "src/secret2.py", 1, 1, "line one"),
    ]
    run_id = run_with(ctx, repo_project, records)
    resp = client.post(f"/api/v1/runs/{run_id}/findings/verify")
    assert resp.status_code == 200, resp.text
    citation = citation_of(client, run_id, "F1")
    assert citation["verdict"]["verdict"] == "quote_matches"

    resp2 = _content(client, citation["id"], before=0, after=1)
    assert resp2.status_code == 200, resp2.text
    body = resp2.json()
    texts = [t for _, t in body["lines"]]
    assert not any(LEAK in t for t in texts)
    assert any("[REDACTED:" in t for t in texts)
    assert body["redacted"] is True


def test_content_caps_a_citation_with_no_line_range(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    """A file citation that names no lines (and whose verdict records no
    ``line_range``) still never returns more than ``MAX_CONTENT_LINES`` rows: it serves the
    first 800 lines of the retained read and says so with ``clipped: true``."""
    big = "\n".join(f"line {i}" for i in range(1, 1001)) + "\n"
    records = [
        *read_records("toolu_big3", "src/big3.py", big),
        link_finding("F1", [{"path": "src/big3.py", "quote": "this text is not in the file"}]),
    ]
    run_id = run_with(ctx, repo_project, records)
    resp = client.post(f"/api/v1/runs/{run_id}/findings/verify")
    assert resp.status_code == 200, resp.text
    citation = citation_of(client, run_id, "F1")
    assert citation["line_start"] is None
    assert citation["verdict"]["verdict"] == "quote_not_found"

    resp2 = _content(client, citation["id"], context=200)
    assert resp2.status_code == 200, resp2.text
    body = resp2.json()
    assert body["cited"] is None
    assert body["clipped"] is True
    assert len(body["lines"]) == 800
    assert body["lines"][0][0] == 1
    assert body["lines"][-1][0] == 800
    assert body["window"] == [1, 800]
    assert body["line_first"] == 1
    assert body["line_last"] == 1000


def test_content_no_line_range_short_read_is_whole_and_unclipped(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    records = [
        *read_records("toolu_short", "src/notes.py", NOTES_TEN),
        link_finding("F1", [{"path": "src/notes.py", "quote": "not in the file"}]),
    ]
    run_id = run_with(ctx, repo_project, records)
    resp = client.post(f"/api/v1/runs/{run_id}/findings/verify")
    assert resp.status_code == 200, resp.text
    citation = citation_of(client, run_id, "F1")

    body = _content(client, citation["id"]).json()
    assert body["clipped"] is False
    assert [n for n, _ in body["lines"]] == list(range(1, 11))


def test_content_context_never_pushes_a_response_past_the_cap(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    """The cap is on the whole response, not only the cited range: a 700-line cited range
    asked for with 200 lines of context each side gets at most 800 rows, every cited line
    kept and the context shrunk to fit. Nothing cited was withheld, so it is not clipped."""
    big = "\n".join(f"line {i}" for i in range(1, 1201)) + "\n"
    records = [
        *read_records("toolu_big4", "src/big4.py", big),
        finding_record("F1", "src/big4.py", 251, 950, "this text is not in the file"),
    ]
    run_id = run_with(ctx, repo_project, records)
    resp = client.post(f"/api/v1/runs/{run_id}/findings/verify")
    assert resp.status_code == 200, resp.text
    citation = citation_of(client, run_id, "F1")

    body = _content(client, citation["id"], context=200).json()
    numbers = [n for n, _ in body["lines"]]
    assert len(numbers) == 800
    assert numbers[0] == 201
    assert numbers[-1] == 1000
    assert body["clipped"] is False
    assert body["window"] == [201, 1000]
