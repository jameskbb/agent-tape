"""Reproducible re-execution end to end.

A user-triggered re-run of a recorded, read-only search argv is queued, executed later by a
worker job in a detached (branchless) snapshot of the run's own recorded commit, through the
same command policy every other command replay goes through, and recorded as a separate
``citation_reproductions`` row that never touches the citation's own verdict.
"""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agenttape.application.context import AppContext
from agenttape.application.schemas.catalog import ProjectCreate, ProjectSettings
from agenttape.application.services.projects import ProjectService
from agenttape.application.services.reproductions import ReproductionService
from agenttape.git.manager import WorktreeManager
from agenttape.infrastructure.db.extra_models.findings import CitationVerdict
from agenttape.infrastructure.db.extra_models.git_workspaces import RunWorkspace
from agenttape.infrastructure.db.extra_models.verification import CitationReproduction
from agenttape.infrastructure.db.models import AuditEntry
from agenttape.worker.worker import Worker
from tests.integration.helpers import (
    GREETER_TEXT,
    OTHER_TEXT,
    bash_tool_records,
    citation_of,
    command_record,
    event_finding_record,
    finding_record,
    findings_of,
    race_two_contexts,
    read_records,
)
from tests.integration.test_context_evidence import git, run_with


def _verdict_id(client: TestClient, run_id: str, key: str) -> str | None:
    verdict = citation_of(client, run_id, key)["verdict"]
    return verdict["id"] if verdict else None


def test_search_citation_is_reproduced_automatically(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    """A cited search is re-run automatically once its run finishes; a cited test
    command is not - a user must ask for that one."""
    quote = "src/greeter.py:1:def greet():"
    records = [
        *read_records("toolu_other", "src/other.py", OTHER_TEXT),
        command_record(
            "cmd_s", ["grep", "-rn", "def greet", "src"], cwd=".", exit_code=0, output=quote
        ),
        command_record(
            "cmd_t", ["python3", "-m", "compileall", "-q", "src"], cwd=".", exit_code=0, output=""
        ),
        event_finding_record("F1", "cmd_s", quote),
        {
            "type": "finding",
            "payload": {
                "id": "F2",
                "kind": "claim",
                "text": "cmd_t says something",
                "links": [{"event_ref": "cmd_t"}],
            },
        },
    ]
    run_id = run_with(ctx, repo_project, records)
    # run_with's drive loop usually already executes the automatic reproduction job queued
    # when the run finishes; run once more so this test never depends on that timing.
    asyncio.run(Worker(ctx, worker_id="repro-auto-followup", concurrency=1).run_until_idle())

    citation1 = citation_of(client, run_id, "F1")
    assert citation1["reproduction"] is not None
    assert citation1["reproduction"]["trigger"] == "automatic"
    assert citation1["reproduction"]["status"] == "done"
    assert citation1["reproduction"]["result"] == "reproduced"
    assert citation1["reproductions"] == 1
    verdict1 = citation1["verdict"]
    assert verdict1 is not None

    with ctx.session() as session:
        verdict_rows = session.scalars(
            select(CitationVerdict).where(CitationVerdict.citation_id == citation1["id"])
        ).all()
    assert len(verdict_rows) == 1, "reproducing a search must never write a citation verdict"

    citation2 = citation_of(client, run_id, "F2")
    assert citation2["verdict"] is not None
    assert citation2["verdict"]["evidence"]["not_checkable_reason"] == "no_quote"
    assert citation2["reproduction"] is None
    assert citation2["reproductions"] == 0

    assert ReproductionService(ctx).request_automatic(run_id) == 0


def test_claude_code_bash_grep_is_reproduced_automatically(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    """A Claude Code Bash tool call whose recorded command is a simple search is replayed
    as argv, never through a shell; its working directory was never recorded, so
    the re-run assumes the repository root and says so."""
    quote = "src/greeter.py:1:def greet():"
    records = [
        *bash_tool_records("bash_grep", "grep -rn 'def greet' src", output=quote),
        event_finding_record("F1", "bash_grep", quote),
    ]
    run_id = run_with(ctx, repo_project, records)
    asyncio.run(Worker(ctx, worker_id="repro-bash-auto", concurrency=1).run_until_idle())

    citation = citation_of(client, run_id, "F1")
    reproduction = citation["reproduction"]
    assert reproduction is not None
    assert reproduction["trigger"] == "automatic"
    assert reproduction["status"] == "done"
    assert reproduction["result"] == "reproduced"

    with ctx.session() as session:
        row = session.scalars(
            select(CitationReproduction).where(CitationReproduction.citation_id == citation["id"])
        ).one()
        evidence = row.evidence
    assert evidence.get("cwd_assumed") is True


def test_a_path_qualified_search_program_is_never_run(
    ctx: AppContext, client: TestClient, repo_project: str, tmp_path: Path
) -> None:
    """A recorded command whose program is named by a path outside the
    repository (planted during the run) is never re-run, automatically or on request; the
    reproduction records could_not_run with a named reason and the planted program never
    executes."""
    evil_dir = tmp_path / "evil"
    evil_dir.mkdir()
    evil_grep = evil_dir / "grep"
    marker = tmp_path / "planted-grep-ran"
    evil_grep.write_text(f"#!/bin/sh\ntouch {marker}\necho planted\n")
    evil_grep.chmod(0o755)

    quote = "planted"
    records = [
        *bash_tool_records("bash_evil", f"{evil_grep} TODO src", output=quote),
        event_finding_record("F1", "bash_evil", quote),
    ]
    run_id = run_with(ctx, repo_project, records)
    asyncio.run(Worker(ctx, worker_id="repro-evil-auto", concurrency=1).run_until_idle())

    citation = citation_of(client, run_id, "F1")
    reproduction = citation["reproduction"]
    assert reproduction is not None
    assert reproduction["trigger"] == "automatic"
    assert reproduction["status"] == "done"
    assert reproduction["result"] == "could_not_run"
    assert "named by a path" in reproduction["reason"]
    assert not marker.exists(), "the planted program must never actually run"

    latest = _run_and_get_latest(ctx, client, citation["id"], worker_id="repro-evil-manual")
    assert latest["trigger"] == "user"
    assert latest["status"] == "done"
    assert latest["result"] == "could_not_run"
    assert "named by a path" in latest["reason"]
    assert not marker.exists(), "the planted program must never run on request either"


def test_a_bare_program_planted_on_the_workers_path_is_never_run(
    ctx: AppContext,
    client: TestClient,
    repo_project: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare program name resolves only in the trusted system directories
    (REPLAY_PATH), never through the worker's inherited PATH. An agent that plants
    ``grep`` in a user-writable directory early on PATH (``~/.local/bin``) and then cites a
    plain ``grep`` gets the system grep re-run, and its own program never executes."""
    planted_dir = tmp_path / "home-local-bin"
    planted_dir.mkdir()
    marker = tmp_path / "planted-bare-grep-ran"
    planted = planted_dir / "grep"
    planted.write_text(f"#!/bin/sh\ntouch {marker}\necho 'src/greeter.py:1:def greet():'\n")
    planted.chmod(0o755)
    monkeypatch.setenv("PATH", f"{planted_dir}:{os.environ.get('PATH', '')}")

    quote = "src/greeter.py:1:def greet():"
    records = [
        *bash_tool_records("bash_bare", "grep -rn 'def greet' src", output=quote),
        event_finding_record("F1", "bash_bare", quote),
    ]
    run_id = run_with(ctx, repo_project, records)
    asyncio.run(Worker(ctx, worker_id="repro-bare-auto", concurrency=1).run_until_idle())

    reproduction = citation_of(client, run_id, "F1")["reproduction"]
    assert reproduction is not None
    assert reproduction["status"] == "done"
    assert reproduction["result"] == "reproduced", reproduction
    assert not marker.exists(), "the planted program must never run"


def test_piped_command_is_not_replayed(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    """Shell syntax (a pipe here) is refused before anything runs, automatically or on
    request; the reason names the character."""
    quote = "src/greeter.py:1:def greet():"
    records = [
        *bash_tool_records("bash_pipe", "grep -rn greet src | head -1", output=quote),
        event_finding_record("F1", "bash_pipe", quote),
    ]
    run_id = run_with(ctx, repo_project, records)
    asyncio.run(Worker(ctx, worker_id="repro-pipe-auto", concurrency=1).run_until_idle())

    citation = citation_of(client, run_id, "F1")
    assert citation["reproduction"] is None
    assert citation["reproductions"] == 0

    latest = _run_and_get_latest(ctx, client, citation["id"], worker_id="repro-pipe-manual")
    assert latest["status"] == "done"
    assert latest["result"] == "could_not_run"
    assert "|" in latest["reason"]

    manager = WorktreeManager(ctx.settings.workspaces_dir)
    assert not (manager.root / latest["id"]).exists()


def test_search_reproduced(ctx: AppContext, client: TestClient, repo_project: str) -> None:
    quote = "src/greeter.py:1:def greet():"
    records = [
        *read_records("toolu_other", "src/other.py", OTHER_TEXT),
        command_record(
            "cmd_s", ["grep", "-rn", "def greet", "src"], cwd=".", exit_code=0, output=quote
        ),
        event_finding_record("F1", "cmd_s", quote),
    ]
    run_id = run_with(ctx, repo_project, records)

    [entry] = [f for f in findings_of(client, run_id) if f["key"] == "F1"]
    [citation] = entry["citations"]
    citation_id = citation["id"]
    verdict_before = citation["verdict"]
    verdict_id_before = verdict_before["id"] if verdict_before else None

    resp = client.post(f"/api/v1/citations/{citation_id}/reproductions")
    assert resp.status_code == 202, resp.text
    queued = resp.json()
    assert queued["status"] == "queued"
    assert queued["trigger"] == "user"
    assert queued["mode"] == "search"

    asyncio.run(Worker(ctx, worker_id="repro-test", concurrency=1).run_until_idle())

    latest = _latest_reproduction(ctx, citation_id)
    embedded = citation_of(client, run_id, "F1")
    assert embedded["reproduction"]["id"] == latest["id"]
    assert embedded["reproduction"]["result"] == "reproduced"
    assert latest["status"] == "done"
    assert latest["result"] == "reproduced"
    assert latest["exit_code"] == 0
    assert latest["argv"] == ["grep", "-rn", "def greet", "src"]

    with ctx.session() as session:
        workspace = session.scalars(
            select(RunWorkspace).where(RunWorkspace.run_id == run_id)
        ).first()
        assert workspace is not None
        base_commit = workspace.base_commit
        repo_path = Path(workspace.repo_path) if workspace.repo_path else None
    assert latest["pinned_commit"] == base_commit
    assert repo_path is not None

    manager = WorktreeManager(ctx.settings.workspaces_dir)
    assert not (manager.root / latest["id"]).exists()

    worktree_listing = git(repo_path, "worktree", "list", "--porcelain")
    assert worktree_listing.count("worktree ") == 1
    branch_listing = git(repo_path, "branch", "--list", "agenttape/*")
    assert branch_listing.strip() == ""

    entry_after = next(f for f in findings_of(client, run_id) if f["key"] == "F1")
    verdict_after = entry_after["citations"][0]["verdict"]
    verdict_id_after = verdict_after["id"] if verdict_after else None
    assert verdict_id_after == verdict_id_before

    with ctx.session() as session:
        verdict_rows = session.scalars(
            select(CitationVerdict).where(CitationVerdict.citation_id == citation_id)
        ).all()
    assert len(verdict_rows) == (1 if verdict_id_before else 0)


def _latest_reproduction(ctx: AppContext, citation_id: str) -> dict[str, Any]:
    """The citation's newest ``citation_reproductions`` row, every column.

    The findings payload embeds only the latest row's summary (``reproduction``) and the
    count (``reproductions``); tests that check the stored argv or evidence read the row.
    """
    with ctx.session() as session:
        row = session.scalars(
            select(CitationReproduction)
            .where(CitationReproduction.citation_id == citation_id)
            .order_by(CitationReproduction.created_at.desc(), CitationReproduction.id.desc())
        ).first()
        assert row is not None, citation_id
        return {c.name: getattr(row, c.name) for c in CitationReproduction.__table__.columns}


def _run_and_get_latest(
    ctx: AppContext, client: TestClient, citation_id: str, *, worker_id: str
) -> dict[str, Any]:
    resp = client.post(f"/api/v1/citations/{citation_id}/reproductions")
    assert resp.status_code == 202, resp.text
    asyncio.run(Worker(ctx, worker_id=worker_id, concurrency=1).run_until_idle())
    return _latest_reproduction(ctx, citation_id)


def test_manual_reproduce(ctx: AppContext, client: TestClient) -> None:
    """A user-triggered test command re-run is compared by exit status only."""
    argv = ["python3", "-m", "compileall", "-q", "src"]

    def _project(name: str) -> str:
        repo = Path(ctx.settings.allowed_roots[0]) / name
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "greeter.py").write_text(GREETER_TEXT)
        git(repo, "init", "--quiet", "--initial-branch=main")
        git(repo, "add", "-A")
        git(repo, "commit", "--quiet", "-m", "init")
        return (
            ProjectService(ctx)
            .create(
                ProjectCreate(
                    name=name,
                    repo_path=str(repo),
                    settings=ProjectSettings(validation_commands=[argv]),
                )
            )
            .id
        )

    matching_project = _project("manual-repo-match")
    run_id = run_with(
        ctx,
        matching_project,
        [
            command_record("cmd_c", argv, cwd=".", exit_code=0, output=""),
            event_finding_record("F1", "cmd_c", ""),
        ],
    )
    citation_id = citation_of(client, run_id, "F1")["id"]
    verdict_before = _verdict_id(client, run_id, "F1")
    latest = _run_and_get_latest(ctx, client, citation_id, worker_id="repro-manual-1")
    assert latest["mode"] == "command"
    assert latest["result"] == "reproduced"
    assert _verdict_id(client, run_id, "F1") == verdict_before

    mismatched_project = _project("manual-repo-mismatch")
    run_id2 = run_with(
        ctx,
        mismatched_project,
        [
            command_record("cmd_c", argv, cwd=".", exit_code=1, output=""),
            event_finding_record("F1", "cmd_c", ""),
        ],
    )
    citation_id2 = citation_of(client, run_id2, "F1")["id"]
    latest2 = _run_and_get_latest(ctx, client, citation_id2, worker_id="repro-manual-2")
    assert latest2["result"] == "not_reproduced"
    assert "1" in latest2["reason"] and "0" in latest2["reason"]


def test_git_grep_is_replayed_in_the_snapshot(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    quote = "src/greeter.py:1:def greet():"
    records = [
        command_record(
            "cmd_g", ["git", "grep", "-n", "def greet"], cwd=".", exit_code=0, output=quote
        ),
        event_finding_record("F1", "cmd_g", quote),
    ]
    run_id = run_with(ctx, repo_project, records)
    citation_id = citation_of(client, run_id, "F1")["id"]
    latest = _run_and_get_latest(ctx, client, citation_id, worker_id="repro-git-grep")
    assert latest["mode"] == "search"
    assert latest["result"] == "reproduced"
    assert latest["exit_code"] == 0


def test_not_reproduced_when_output_differs(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    recorded_output = "src/greeter.py:1:def greet():\nsrc/other.py:1:def greet():"
    records = [
        command_record(
            "cmd_s",
            ["grep", "-rn", "def greet", "src"],
            cwd=".",
            exit_code=0,
            output=recorded_output,
        ),
        event_finding_record("F1", "cmd_s", "src/greeter.py:1:def greet():"),
    ]
    run_id = run_with(ctx, repo_project, records)
    citation_id = citation_of(client, run_id, "F1")["id"]
    latest = _run_and_get_latest(ctx, client, citation_id, worker_id="repro-diff")
    assert latest["result"] == "not_reproduced"


def test_could_not_run_reasons(ctx: AppContext, client: TestClient, repo_project: str) -> None:
    records = [
        *read_records("toolu_read", "src/other.py", OTHER_TEXT),
        command_record(
            "cmd_curl", ["curl", "https://example.com"], cwd=".", exit_code=0, output=""
        ),
        command_record("cmd_rm", ["rm", "-rf", "src"], cwd=".", exit_code=0, output=""),
        command_record(
            "cmd_outside", ["grep", "-rn", "x", "/etc"], cwd=".", exit_code=0, output=""
        ),
        command_record("cmd_pre", ["rg", "--pre", "cat", "x"], cwd=".", exit_code=0, output=""),
        {
            "type": "command",
            "payload": {
                "argv": ["grep", "-rn", "x", "src"],
                "cwd": ".",
                "exit_code": 0,
                "output": "src/greeter.py:1:x",
                "output_truncated": True,
                "tool_use_id": "cmd_truncated",
            },
        },
        event_finding_record("F_curl", "cmd_curl", ""),
        event_finding_record("F_rm", "cmd_rm", ""),
        event_finding_record("F_outside", "cmd_outside", ""),
        event_finding_record("F_pre", "cmd_pre", ""),
        event_finding_record("F_truncated", "cmd_truncated", ""),
        event_finding_record("F_read", "toolu_read", ""),
    ]
    run_id = run_with(ctx, repo_project, records)

    manager = WorktreeManager(ctx.settings.workspaces_dir)
    keys = ("F_curl", "F_rm", "F_outside", "F_pre", "F_truncated", "F_read")
    verdicts_before = {key: _verdict_id(client, run_id, key) for key in keys}
    citation_ids = {key: citation_of(client, run_id, key)["id"] for key in keys}
    for key, citation_id in citation_ids.items():
        resp = client.post(f"/api/v1/citations/{citation_id}/reproductions")
        assert resp.status_code == 202, (key, resp.text)

    asyncio.run(Worker(ctx, worker_id="repro-could-not-run", concurrency=1).run_until_idle())

    for key, citation_id in citation_ids.items():
        latest = _latest_reproduction(ctx, citation_id)
        assert latest["status"] == "done", (key, latest)
        assert latest["result"] == "could_not_run", (key, latest)
        assert latest["reason"], key
        assert not (manager.root / latest["id"]).exists()
        assert _verdict_id(client, run_id, key) == verdicts_before[key]


def test_a_second_request_while_one_is_pending_conflicts(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    quote = "src/greeter.py:1:def greet():"
    records = [
        command_record(
            "cmd_s", ["grep", "-rn", "def greet", "src"], cwd=".", exit_code=0, output=quote
        ),
        event_finding_record("F1", "cmd_s", quote),
    ]
    run_id = run_with(ctx, repo_project, records)
    citation_id = citation_of(client, run_id, "F1")["id"]

    first = client.post(f"/api/v1/citations/{citation_id}/reproductions")
    assert first.status_code == 202, first.text
    second = client.post(f"/api/v1/citations/{citation_id}/reproductions")
    assert second.status_code == 409, second.text


def test_concurrent_requests_create_only_one_pending_reproduction(
    ctx: AppContext, client: TestClient, repo_project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two requests racing through two contexts both pass the service's own
    "nothing pending" check before either inserts (a barrier holds them there); the
    database's one-pending-per-citation index lets exactly one queued row in, and the loser
    gets the same 409 conflict a sequential second request does."""
    argv = ["python3", "-m", "compileall", "-q", "src"]
    run_id = run_with(
        ctx,
        repo_project,
        [
            command_record("cmd_c", argv, cwd=".", exit_code=0, output=""),
            event_finding_record("F1", "cmd_c", ""),
        ],
    )
    citation_id = citation_of(client, run_id, "F1")["id"]

    barrier = threading.Barrier(2, timeout=15)
    # The private _extract_recorded_command is the narrowest seam between the pending check
    # and the insert, so pausing there is what makes this race deterministic.
    original = ReproductionService._extract_recorded_command

    def extract_after_both_checked(*args: Any, **kwargs: Any) -> Any:
        barrier.wait()  # both requests have already found nothing pending
        return original(*args, **kwargs)

    monkeypatch.setattr(
        ReproductionService,
        "_extract_recorded_command",
        staticmethod(extract_after_both_checked),
    )
    outcomes = race_two_contexts(
        ctx, lambda c: ReproductionService(c).request(citation_id, trigger="user")
    )
    kinds = sorted(kind for kind, _ in outcomes)
    assert kinds == ["conflict", "ok"], outcomes
    with ctx.session() as session:
        rows = session.scalars(
            select(CitationReproduction).where(CitationReproduction.citation_id == citation_id)
        ).all()
        assert [row.status for row in rows] == ["queued"]

    # The conflict is the API's own 409 while the winner is still pending.
    monkeypatch.setattr(ReproductionService, "_extract_recorded_command", original)
    again = client.post(f"/api/v1/citations/{citation_id}/reproductions")
    assert again.status_code == 409, again.text


def test_reproduction_is_audited(ctx: AppContext, client: TestClient, repo_project: str) -> None:
    quote = "src/greeter.py:1:def greet():"
    records = [
        command_record(
            "cmd_s", ["grep", "-rn", "def greet", "src"], cwd=".", exit_code=0, output=quote
        ),
        event_finding_record("F1", "cmd_s", quote),
    ]
    run_id = run_with(ctx, repo_project, records)
    citation_id = citation_of(client, run_id, "F1")["id"]

    resp = client.post(
        f"/api/v1/citations/{citation_id}/reproductions",
        headers={"X-AgentTape-Actor": "Jamie"},
    )
    assert resp.status_code == 202, resp.text

    with ctx.session() as session:
        entries = session.scalars(
            select(AuditEntry).where(AuditEntry.action == "citation.reproduce")
        ).all()
    assert len(entries) == 1
    assert entries[0].actor == "Jamie"
    assert entries[0].resource_id == citation_id


def test_a_file_citation_cannot_be_re_run(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    quote = "def greet():\n    return 'hi'"
    records = [
        *read_records("toolu_f", "src/greeter.py", GREETER_TEXT),
        finding_record("F1", "src/greeter.py", 1, 2, quote),
    ]
    run_id = run_with(ctx, repo_project, records)
    citation_id = citation_of(client, run_id, "F1")["id"]

    resp = client.post(f"/api/v1/citations/{citation_id}/reproductions")
    assert resp.status_code == 422, resp.text


def test_a_cited_mutating_git_command_is_never_replayed(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    """A citation of ``git branch <name>``, ``git worktree add``, or ``git
    worktree move`` records could_not_run with a named refusal before the command policy is
    consulted, and the run's repository keeps exactly the branches and worktrees it had."""
    records = [
        command_record(
            "cmd_branch", ["git", "branch", "experimental"], cwd=".", exit_code=0, output=""
        ),
        command_record(
            "cmd_wt_add",
            ["git", "worktree", "add", "src/extra-worktree", "HEAD"],
            cwd=".",
            exit_code=0,
            output="",
        ),
        command_record(
            "cmd_wt_move", ["git", "worktree", "move", "a", "b"], cwd=".", exit_code=0, output=""
        ),
        *bash_tool_records("bash_branch", "git branch from-bash", output=""),
        event_finding_record("F_branch", "cmd_branch", ""),
        event_finding_record("F_wt_add", "cmd_wt_add", ""),
        event_finding_record("F_wt_move", "cmd_wt_move", ""),
        event_finding_record("F_bash", "bash_branch", ""),
    ]
    run_id = run_with(ctx, repo_project, records)
    with ctx.session() as session:
        workspace = session.scalars(
            select(RunWorkspace).where(RunWorkspace.run_id == run_id)
        ).first()
        assert workspace is not None and workspace.repo_path
        repo_path = Path(workspace.repo_path)

    def refs_and_worktrees() -> tuple[str, str]:
        return (
            git(repo_path, "for-each-ref", "--format=%(refname)"),
            git(repo_path, "worktree", "list", "--porcelain"),
        )

    before = refs_and_worktrees()
    keys = ("F_branch", "F_wt_add", "F_wt_move", "F_bash")
    citation_ids = {key: citation_of(client, run_id, key)["id"] for key in keys}
    for key, citation_id in citation_ids.items():
        resp = client.post(f"/api/v1/citations/{citation_id}/reproductions")
        assert resp.status_code == 202, (key, resp.text)
    asyncio.run(Worker(ctx, worker_id="repro-mutating-git", concurrency=1).run_until_idle())

    manager = WorktreeManager(ctx.settings.workspaces_dir)
    for key, citation_id in citation_ids.items():
        latest = _latest_reproduction(ctx, citation_id)
        assert latest["status"] == "done", (key, latest)
        assert latest["result"] == "could_not_run", (key, latest)
        assert "not one of the read-only git subcommands" in latest["reason"], (key, latest)
        assert latest["evidence"]["refusal"] == "git_subcommand_not_replayable", (key, latest)
        assert "policy" not in latest["evidence"], "refused before the command policy"
        assert latest["pinned_commit"] is None, "no snapshot was ever prepared"
        assert not (manager.root / latest["id"]).exists()

    after = refs_and_worktrees()
    assert after == before
    assert "experimental" not in after[0] and "from-bash" not in after[0]
    assert after[1].count("worktree ") == 1


def test_a_git_global_option_is_never_replayed(
    ctx: AppContext, client: TestClient, repo_project: str
) -> None:
    """``git -C <dir>`` and ``--work-tree=<dir>`` with an in-repository value
    are refused by name in command mode too, not only when the value looks like an outside
    path."""
    records = [
        command_record("cmd_c", ["git", "-C", "src", "log", "-1"], cwd=".", exit_code=0, output=""),
        command_record(
            "cmd_wt", ["git", "--work-tree=src", "status"], cwd=".", exit_code=0, output=""
        ),
        event_finding_record("F_c", "cmd_c", ""),
        event_finding_record("F_wt", "cmd_wt", ""),
    ]
    run_id = run_with(ctx, repo_project, records)
    for key in ("F_c", "F_wt"):
        citation_id = citation_of(client, run_id, key)["id"]
        latest = _run_and_get_latest(ctx, client, citation_id, worker_id=f"repro-global-{key}")
        assert latest["mode"] == "command", (key, latest)
        assert latest["result"] == "could_not_run", (key, latest)
        assert latest["evidence"]["refusal"] == "git_global_option_not_replayable", (key, latest)
        assert "policy" not in latest["evidence"]


def test_an_internal_reproduction_error_stores_no_exception_text(
    ctx: AppContext, client: TestClient, repo_project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bug inside the reproduction service becomes a generic could_not_run reason plus the
    error type; the exception text (which can name host paths) stays in the server log."""
    quote = "src/greeter.py:1:def greet():"
    records = [
        command_record(
            "cmd_s", ["grep", "-rn", "def greet", "src"], cwd=".", exit_code=0, output=quote
        ),
        event_finding_record("F1", "cmd_s", quote),
    ]
    run_id = run_with(ctx, repo_project, records)
    citation_id = citation_of(client, run_id, "F1")["id"]
    service = ReproductionService(ctx)
    queued = service.request(citation_id, trigger="user")

    async def broken_run(*args: Any, **kwargs: Any) -> Any:
        # _run is everything execute() guards; raising here is the "bug in this code" case.
        raise RuntimeError("/home/someone/secret-path exploded")

    monkeypatch.setattr(ReproductionService, "_run", broken_run)
    asyncio.run(service.execute(queued.id))

    with ctx.session() as session:
        row = session.get(CitationReproduction, queued.id)
        assert row is not None
        assert (row.status, row.result) == ("done", "could_not_run")
        assert row.evidence == {"internal_error": "RuntimeError"}
        assert "secret-path" not in (row.reason or "")
