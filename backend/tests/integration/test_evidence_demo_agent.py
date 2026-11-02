"""The real evidence demo agent (``full_evidence``), run locally through the worker.

This is the seed's contract: the seeded "AgentTape Evidence Demo" project and profile run
``python -m agenttape.demo_agent`` in an isolated worktree, pause on the protected-path
approval, resume, and leave the file ledger the seeded demo is meant to show.
"""

from __future__ import annotations

import json
import shutil
from collections import Counter
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agenttape import demo_agent
from agenttape.adapters.builtin._demo_repo import ensure_fixture_repo
from agenttape.application.context import AppContext
from agenttape.domain.enums import RunStatus
from agenttape.infrastructure.db.extra_models.file_evidence import (
    FileEvidenceEvent,
    RunFileSummary,
)
from agenttape.infrastructure.db.models import ApprovalRequest, Event
from agenttape.seeds import file_evidence_demo
from agenttape.seeds.base import SeedReport
from tests.conftest import drive, start_run


@pytest.mark.slow
def test_full_evidence_demo_ledger(ctx: AppContext, client: TestClient) -> None:
    report = SeedReport()
    project_id = file_evidence_demo._project(ctx, report)
    profile_id = file_evidence_demo._profiles(ctx, report, project_id)["a"]
    run_id = start_run(ctx, profile_id, project_id=project_id)

    assert drive(ctx, run_id, timeout=120).run.status == RunStatus.WAITING_FOR_APPROVAL
    # The worker owns a paused run's evidence: a manual reconcile would race the resume.
    refused = client.post(f"/api/v1/runs/{run_id}/files/rebuild", params={"reconcile": "true"})
    assert refused.status_code == 409
    assert file_evidence_demo.approve_evidence_runs(ctx, [run_id]) == 1
    assert drive(ctx, run_id, timeout=120).run.status == RunStatus.SUCCEEDED

    with ctx.session() as session:
        rows = {
            r.file_key: r
            for r in session.scalars(select(RunFileSummary).where(RunFileSummary.run_id == run_id))
        }
        read_sources = set(
            session.scalars(
                select(FileEvidenceEvent.source).where(
                    FileEvidenceEvent.run_id == run_id, FileEvidenceEvent.operation == "read"
                )
            )
        )
        approval = session.scalars(
            select(ApprovalRequest).where(ApprovalRequest.run_id == run_id)
        ).one()
        #: What the agent itself reported (its writes, renames, reads and searches) and the
        #: commands it ran: exact, unlike the watcher's per-batch counts.
        agent_ops = Counter(
            (e.path, e.operation, e.source)
            for e in session.scalars(
                select(FileEvidenceEvent).where(
                    FileEvidenceEvent.run_id == run_id,
                    FileEvidenceEvent.source.in_(("agent_hook", "tool_event", "command_reference")),
                )
            )
        )
        commands = [
            e.payload
            for e in session.scalars(
                select(Event).where(Event.run_id == run_id, Event.type == "command")
            )
            if e.payload.get("reported_by") == "agent"
        ]
        session.expunge_all()

    # Modified and kept: one agent write plus the formatter's rewrite. The agent reports its
    # own write; the formatter's rewrite is seen by the watcher, whose per-batch count
    # depends on scheduling, so the ledger's modify count is a lower bound here. `reference`
    # is 2: the formatter's argv and the search command's argv both name it.
    greeter = rows["src/greeter.py"]
    assert greeter.classification == "modified" and greeter.net_status == "modified"
    assert agent_ops[("src/greeter.py", "modify", "agent_hook")] == 1
    assert agent_ops[("src/greeter.py", "read", "tool_event")] == 1
    assert agent_ops[("src/greeter.py", "search", "agent_hook")] == 1
    assert agent_ops[("src/greeter.py", "reference", "command_reference")] == 2
    assert greeter.operation_counts["modify"] >= 2
    assert {k: v for k, v in greeter.operation_counts.items() if k != "modify"} == {
        "read": 1,
        "reference": 2,
        "search": 1,
    }

    # Modified, then restored to its original bytes.
    readme = rows["README.md"]
    assert readme.classification == "restored", readme.explanation
    assert readme.net_unchanged and readme.net_status == "unchanged"
    assert "git_end" not in readme.sources  # no stale provisional reconciliation

    # Created, then renamed: one logical file with its chain.
    formatting = rows["src/formatting.py"]
    assert formatting.rename_chain == ["src/helpers_draft.py", "src/formatting.py"]
    assert formatting.classification == "created" and formatting.net_status == "added"
    assert agent_ops[("src/helpers_draft.py", "create", "agent_hook")] == 1
    assert agent_ops[("src/formatting.py", "rename", "agent_hook")] == 1
    assert formatting.operation_counts["create"] >= 1 and formatting.operation_counts["rename"] >= 1
    assert "src/helpers_draft.py" not in rows

    # Created, then deleted.
    scratch = rows["scratch_notes.txt"]
    assert scratch.classification == "net_unchanged" and scratch.net_unchanged

    # Written only by the test command: generated.
    junit = rows["reports/junit.xml"]
    assert junit.classification == "generated" and junit.is_generated
    assert any("<demo tests script>" in t for t in junit.tools), junit.tools

    # Rewritten by the formatter with identical bytes: touched, net unchanged.
    assert rows["src/__init__.py"].classification == "net_unchanged"

    # Protected path written after approval.
    ci = rows[".github/workflows/ci.yml"]
    assert ci.is_protected and ci.classification == "created"
    assert ci.policy is not None and ci.policy["decision"] == "approved"
    assert ci.policy["approval_id"] == approval.id
    assert approval.type == "protected_path" and approval.status == "approved"

    # A protected file that was only read is not a protected finding.
    changelog = rows["CHANGELOG.md"]
    assert changelog.is_protected and changelog.classification == "observed_read"
    summary = client.get(f"/api/v1/runs/{run_id}/files/summary").json()
    assert [f["file_key"] for f in summary["protected"]] == [".github/workflows/ci.yml"]
    assert summary["completeness"]["level"] == "complete"

    # 300 installed files collapse into one noisy-directory row.
    assert rows["node_modules/"].is_collapsed and (rows["node_modules/"].collapsed_count or 0) > 0
    assert not any(k.startswith("node_modules/left-pad") for k in rows)

    # Reads come only from agent telemetry: reported reads and Read tool calls, never from
    # the watcher.
    assert read_sources == {"agent_hook", "tool_event"}

    # Commands report the argv that ran (interpreter name, inline script elided).
    formatter = next(c for c in commands if c["category"] == "formatter")
    assert formatter["argv"][1:3] == ["-c", "<demo formatter script>"]
    assert formatter["label"] == "demo formatter"


def _strip_cat_n(output: str) -> str:
    """Reverse the ``read_tool`` ``cat -n`` layout: a 6-wide right-aligned number, a tab,
    then the line."""
    lines = []
    for line in output.splitlines():
        _number, _tab, content = line.partition("\t")
        lines.append(content)
    return "\n".join(lines)


@pytest.mark.slow
def test_demo_agent_telemetry_backs_every_designed_verdict(tmp_path: Path) -> None:
    """Honesty guard: every entry in ``DESIGNED_VERDICTS`` is backed by what the demo agent
    actually recorded, not merely claimed in ``demo_agent.py``."""
    fixture = ensure_fixture_repo(tmp_path / "data")
    workdir = tmp_path / "work"
    shutil.copytree(fixture, workdir)
    telemetry = tmp_path / "telemetry.jsonl"

    code = demo_agent.main(
        [
            "--workdir",
            str(workdir),
            "--telemetry",
            str(telemetry),
            "--phase",
            "start",
            "--step-delay-ms",
            "0",
        ]
    )
    assert code == demo_agent.EXIT_NEEDS_APPROVAL

    records = [
        json.loads(line) for line in telemetry.read_text(encoding="utf-8").splitlines() if line
    ]

    # Pair each "Read" tool_use with its tool_result by id, and recover the read-time
    # content by stripping the cat-n line-number prefix.
    tool_use_by_id = {r["id"]: r for r in records if r.get("type") == "tool_use"}
    read_content: dict[str, str] = {}
    read_index: dict[str, int] = {}
    for idx, r in enumerate(records):
        if r.get("type") != "tool_result":
            continue
        use = tool_use_by_id.get(r.get("tool_use_id"))
        if use is None or use.get("name") != "Read":
            continue
        path = use["input"]["file_path"]
        read_content[path] = _strip_cat_n(str(r.get("output", "")))
        read_index[path] = idx

    bare_read_paths = {r["path"] for r in records if r.get("type") == "file_read"}
    write_or_rename = [
        (idx, r) for idx, r in enumerate(records) if r.get("type") in ("file_write", "file_rename")
    ]
    command_records = [r for r in records if r.get("type") == "command"]
    commands_by_ref = {r["id"]: r for r in command_records if "id" in r}
    findings = {r["id"]: r for r in records if r.get("type") == "finding"}

    def first_write_index(path: str) -> int | None:
        indices = [
            idx for idx, r in write_or_rename if r.get("path") == path or r.get("old_path") == path
        ]
        return min(indices) if indices else None

    def citation_links(finding_id: str) -> list[dict[str, object]]:
        return [
            link
            for link in findings[finding_id]["links"]
            if isinstance(link.get("path"), str) or isinstance(link.get("event_ref"), str)
        ]

    assert set(demo_agent.DESIGNED_VERDICTS.values()) == {
        "stale",
        "quote_matches",
        "quote_not_found",
        "never_read",
        "not_checkable",
        "read_not_observed",
    }

    # F2#1's search ran for real, before anything wrote src/greeter.py.
    search_index = next(
        idx for idx, r in enumerate(records) if r.get("id") == demo_agent.SEARCH_REF
    )
    first_greeter_write = first_write_index("src/greeter.py")
    assert first_greeter_write is not None and search_index < first_greeter_write

    for (finding_id, position), verdict in demo_agent.DESIGNED_VERDICTS.items():
        link = citation_links(finding_id)[position]

        if verdict == "quote_matches" and "event_ref" in link:
            command = commands_by_ref[link["event_ref"]]
            assert link["quote"] in str(command["output"])
            continue

        path = str(link["path"])
        line_start, line_end, quote = link["line_start"], link["line_end"], link["quote"]

        if verdict == "quote_matches":
            content = read_content[path]
            got = "\n".join(content.splitlines()[line_start - 1 : line_end])
            assert got == quote
        elif verdict == "stale":
            content = read_content[path]
            got = "\n".join(content.splitlines()[line_start - 1 : line_end])
            assert got == quote
            write_idx = first_write_index(path)
            assert write_idx is not None and write_idx > read_index[path]
        elif verdict == "quote_not_found":
            content = read_content[path]
            assert quote not in content
        elif verdict == "never_read":
            assert path not in read_content
            assert path not in bare_read_paths
        elif verdict == "not_checkable":
            assert path in bare_read_paths
            assert path not in read_content
            actual = (workdir / path).read_text(encoding="utf-8")
            assert quote in actual  # genuinely in the file; just never recorded
        elif verdict == "read_not_observed":
            assert path not in read_content
            assert path not in bare_read_paths
            assert any(path in (r.get("argv") or []) for r in command_records)
        else:  # pragma: no cover - exhaustiveness guard
            raise AssertionError(f"unhandled verdict {verdict!r}")

    for finding_id in demo_agent.DECLARED_ONLY_FINDINGS:
        assert citation_links(finding_id) == []
