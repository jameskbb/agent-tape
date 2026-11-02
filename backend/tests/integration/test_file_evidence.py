"""File Evidence end to end in temporary Git repositories, through the real worker.

A test adapter runs Python code in the run's isolated worktree (so the filesystem watcher,
the Git start/end snapshots, and reconciliation all run for real) and can emit Demo Agent
telemetry records before its code runs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from agenttape.adapters.base import (
    AdapterResult,
    AgentAdapter,
    DiscoveryResult,
    ExecutionContext,
    ProfileIssue,
)
from agenttape.adapters.builtin.demo import DemoAdapter
from agenttape.adapters.process import CommandSpec, run_process
from agenttape.application.context import AppContext
from agenttape.application.schemas.catalog import (
    AgentProfileCreate,
    ProjectCreate,
    ProjectSettings,
)
from agenttape.application.services.profiles import AgentProfileService
from agenttape.application.services.projects import ProjectService
from agenttape.domain.enums import AdapterAvailability, FailureCategory
from agenttape.file_evidence import store
from agenttape.file_evidence.collector import reconcile_run
from agenttape.file_evidence.ledger import NoisePolicy
from agenttape.infrastructure.db.extra_models.file_evidence import (
    FileEvidenceEvent,
    RunFileSummary,
)
from agenttape.infrastructure.db.models import ApprovalRequest, Event, Run, RunAttempt
from tests.conftest import drive, start_run

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test User",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test User",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}
PAUSE = "time.sleep(0.9)"

#: Modified and kept; modified and restored; created and deleted; renamed twice; untracked
#: create; a formatter rewriting three files with one net change; a dependency install.
STORY = f"""
import os, pathlib, subprocess, sys, time
P = pathlib.Path('.')
(P / 'src' / 'app.py').write_text('print("changed")\\n'); {PAUSE}
orig = (P / 'docs' / 'guide.md').read_text()
(P / 'docs' / 'guide.md').write_text(orig + 'draft\\n'); {PAUSE}
(P / 'docs' / 'guide.md').write_text(orig); {PAUSE}
(P / 'tmp.txt').write_text('scratch'); {PAUSE}
os.remove('tmp.txt'); {PAUSE}
os.rename('OBSOLETE.md', 'a.md'); {PAUSE}
os.rename('a.md', 'b.md'); {PAUSE}
(P / 'notes').mkdir(); (P / 'notes' / 'new.txt').write_text('hello\\n'); {PAUSE}
fmt = "import pathlib, sys\\nfor n in sys.argv[1:]:\\n    p = pathlib.Path(n)\\n    p.write_text(''.join(l.rstrip() + '\\\\n' for l in p.read_text().splitlines()))"
subprocess.run([sys.executable, '-c', fmt, 'src/util.py', 'src/style.py', 'README.md'], check=True)
{PAUSE}
for i in range(400):
    d = P / 'node_modules' / 'pkg' / str(i // 100)
    d.mkdir(parents=True, exist_ok=True)
    (d / f'f{{i}}.js').write_text(str(i))
print('story done')
"""


class EvidenceTestAdapter(AgentAdapter):
    """Emits ``records`` (Demo Agent telemetry), then runs ``code`` in the workdir."""

    name = "evidence_test"
    display_name = "Evidence test agent"

    def discover(self) -> DiscoveryResult:
        return DiscoveryResult(adapter=self.name, status=AdapterAvailability.READY, version="1")

    def validate_profile(self, config: dict[str, Any]) -> list[ProfileIssue]:
        return []

    async def execute(self, ctx: ExecutionContext) -> AdapterResult:
        demo = DemoAdapter()
        for raw in ctx.profile.get("records") or []:
            for draft in demo.normalize(raw):
                await ctx.events.emit(draft)
        result = await run_process(
            CommandSpec(
                argv=[sys.executable, "-c", ctx.profile.get("code") or "pass"], cwd=ctx.workdir
            ),
            ctx,
        )
        if result.stopped_reason is not None:
            return AdapterResult.failure(result.stopped_reason, "stopped")
        if result.exit_code == 0:
            return AdapterResult.success(exit_code=0)
        return AdapterResult.failure(FailureCategory.ADAPTER_ERROR, "failed", exit_code=1)


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, **GIT_ENV}
    out = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, check=True
    )
    return out.stdout.strip()


@pytest.fixture
def repo(ctx: AppContext) -> Path:
    repo = Path(ctx.settings.allowed_roots[0]) / "app"
    for rel, text in {
        "src/app.py": 'print("hello")\n',
        "src/util.py": "VALUE = 1\n",
        "src/style.py": "X = 1   \n",
        "docs/guide.md": "# Guide\n\nText.\n",
        "OBSOLETE.md": "old notes\n",
        "README.md": "# App\n",
        ".github/workflows/ci.yml": "on: push\n",
    }.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    git(repo, "init", "--quiet", "--initial-branch=main")
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", "Initial commit")
    ctx.adapters.register(EvidenceTestAdapter())
    return repo


def project(ctx: AppContext, repo: Path, **file_evidence: Any) -> str:
    return (
        ProjectService(ctx)
        .create(
            ProjectCreate(
                name=f"App {len(file_evidence)} {sorted(file_evidence)}",
                repo_path=str(repo),
                settings=ProjectSettings(
                    git_isolation=True,
                    protected_paths=[".github/**", "*.lock"],
                    **({"file_evidence": file_evidence} if file_evidence else {}),  # type: ignore[arg-type]
                ),
            )
        )
        .id
    )


def run_agent(
    ctx: AppContext, project_id: str, code: str, records: list[dict[str, Any]] | None = None
) -> str:
    profile = (
        AgentProfileService(ctx)
        .create(
            AgentProfileCreate(
                name=f"evidence-{os.urandom(3).hex()}",
                adapter_type="evidence_test",
                config={"code": code, "records": records or []},
            )
        )
        .id
    )
    run_id = start_run(ctx, profile, project_id=project_id)
    drive(ctx, run_id, timeout=90)
    return run_id


def summaries(ctx: AppContext, run_id: str) -> dict[str, RunFileSummary]:
    with ctx.session() as session:
        rows = list(session.scalars(select(RunFileSummary).where(RunFileSummary.run_id == run_id)))
        session.expunge_all()
    return {r.file_key: r for r in rows}


def assert_collapsed_bucket_consistent(ctx: AppContext, run_id: str, bucket: str) -> int:
    """A collapsed row's count is exactly what its watcher rows recorded; returns the count.

    How many events inotify delivered before the watcher stopped depends on scheduling, so
    integration tests check consistency here and the ledger unit tests pin the magnitudes.
    """
    row = summaries(ctx, run_id)[bucket]
    assert row.is_collapsed and row.collapsed_count and row.collapsed_count > 0
    with ctx.session() as session:
        events = list(
            session.scalars(
                select(FileEvidenceEvent).where(
                    FileEvidenceEvent.run_id == run_id,
                    FileEvidenceEvent.operation == "collapsed",
                    FileEvidenceEvent.path == bucket,
                )
            )
        )
    by_operation = [n for e in events for n in e.details["by_operation"].values()]
    assert sum(e.count for e in events) == sum(by_operation) == row.collapsed_count
    assert sum(row.operation_counts.values()) == row.collapsed_count
    return row.collapsed_count


@pytest.mark.slow
def test_file_story_classifications_and_api(
    ctx: AppContext, repo: Path, client: TestClient
) -> None:
    run_id = run_agent(
        ctx,
        project(ctx, repo),
        STORY,
        records=[{"type": "file_read", "path": "docs/guide.md", "bytes": 16}],
    )
    with ctx.session() as session:
        assert session.get(Run, run_id).status == "succeeded"  # type: ignore[union-attr]
    rows = summaries(ctx, run_id)

    kept = rows["src/app.py"]
    assert kept.classification == "modified" and kept.net_status == "modified"
    assert "filesystem_watcher" in kept.sources and kept.insertions == 1

    restored = rows["docs/guide.md"]
    assert restored.classification == "restored", restored.explanation
    assert restored.net_unchanged and restored.net_status == "unchanged"
    assert "observed_read" in restored.classifications  # the telemetry read is kept

    temp = rows["tmp.txt"]
    assert temp.classification == "net_unchanged" and temp.net_unchanged

    renamed = rows["b.md"]
    assert renamed.rename_chain == ["OBSOLETE.md", "a.md", "b.md"]
    assert renamed.classification == "renamed" and renamed.net_status == "renamed"
    assert "OBSOLETE.md" not in rows and "a.md" not in rows

    untracked = rows["notes/new.txt"]
    assert untracked.classification == "created" and untracked.net_status == "added"

    # Formatter: three files rewritten, one net change; the other two were touched only.
    assert rows["src/style.py"].classification == "modified"
    for touched in ("src/util.py", "README.md"):
        assert rows[touched].classification == "net_unchanged", rows[touched].explanation
        assert rows[touched].net_unchanged and rows[touched].sources == ["filesystem_watcher"]

    collapsed_count = assert_collapsed_bucket_consistent(ctx, run_id, "node_modules/")
    assert not any(k.startswith("node_modules/pkg") for k in rows)
    with ctx.session() as session:
        noisy_rows = session.scalar(
            select(func.count(FileEvidenceEvent.id)).where(
                FileEvidenceEvent.run_id == run_id, FileEvidenceEvent.path.like("node_modules%")
            )
        )
    assert noisy_rows is not None and noisy_rows < 20  # counted, never one row per file

    # --- API: summary, filters, timeline, exports, rebuild determinism
    summary = client.get(f"/api/v1/runs/{run_id}/files/summary").json()
    assert summary["completeness"]["level"] == "complete"
    assert summary["completeness"]["statement"].startswith(
        "Writes and final changes fully observed"
    )
    assert summary["counts"]["collapsed_directories"] == 1
    assert summary["counts"]["collapsed_events"] == collapsed_count
    assert summary["counts"]["restored_or_net_unchanged"] >= 4
    page = client.get(f"/api/v1/runs/{run_id}/files", params={"net_unchanged": "true"}).json()
    assert {r["file_key"] for r in page["items"]} >= {"docs/guide.md", "tmp.txt", "README.md"}
    assert all(r["net_unchanged"] for r in page["items"])
    searched = client.get(f"/api/v1/runs/{run_id}/files", params={"q": "obsolete"}).json()
    assert [r["file_key"] for r in searched["items"]] == ["b.md"]
    by_ops = client.get(
        f"/api/v1/runs/{run_id}/files", params={"sort": "operations", "order": "desc", "limit": 2}
    ).json()
    busiest = max(rows.values(), key=lambda r: (r.operation_total, r.file_key))
    assert by_ops["items"][0]["file_key"] == busiest.file_key and by_ops["next_offset"] == 2

    timeline = client.get(
        f"/api/v1/runs/{run_id}/files/timeline", params={"path": "docs/guide.md"}
    ).json()
    ops = [e["operation"] for e in timeline["events"]]
    assert ops[0] == "read" and "modify" in ops
    assert timeline["start_state"]["hash"] == timeline["end_state"]["hash"]
    assert timeline["linked_events"] and timeline["linked_events"][0]["type"] == "file_read"
    diff = client.get(f"/api/v1/runs/{run_id}/files/timeline", params={"path": "src/app.py"}).json()
    assert diff["diff"] is not None and diff["diff"]["insertions"] == 1

    csv_text = client.get(
        f"/api/v1/runs/{run_id}/files/export", params={"format": "csv", "net_unchanged": "true"}
    ).text
    assert csv_text.splitlines()[0].startswith("path,start_path")
    assert "docs/guide.md" in csv_text and "src/app.py" not in csv_text
    bundle = client.get(f"/api/v1/runs/{run_id}/evidence/export").json()
    assert bundle["format"] == "agenttape.evidence-bundle"
    assert bundle["completeness"]["level"] == "complete"
    assert {
        "file_summaries",
        "evidence_events",
        "collapsed_directories",
        "git",
        "runtime",
        "context",
    } <= set(bundle)
    raw = json.dumps(bundle)
    assert str(ctx.settings.data_dir) not in raw and str(repo) not in raw  # no absolute paths

    before = [
        (r.file_key, r.classification, r.net_status, r.operation_total) for r in rows.values()
    ]
    for _ in range(2):
        rebuilt = client.post(f"/api/v1/runs/{run_id}/files/rebuild")
        assert rebuilt.status_code == 200
        after = summaries(ctx, run_id)
        assert [
            (r.file_key, r.classification, r.net_status, r.operation_total) for r in after.values()
        ] == before


def test_protected_write_is_detected_not_prevented_and_needs_review(
    ctx: AppContext, repo: Path
) -> None:
    code = (
        "import pathlib, time\n"
        "pathlib.Path('.github/workflows/ci.yml').write_text('on: pull_request\\n')\n"
        "time.sleep(2.5)\n"
    )
    default_run = run_agent(ctx, project(ctx, repo), code)
    row = summaries(ctx, default_run)[".github/workflows/ci.yml"]
    assert row.is_protected and row.classification == "modified"
    assert row.policy is not None
    assert row.policy["decision"] == "detected" and row.policy["prevented"] is False
    assert row.policy["review_required"] is True and row.policy["cancelled"] is False
    with ctx.session() as session:
        events = list(
            session.scalars(
                select(Event).where(Event.run_id == default_run, Event.type == "file_evidence")
            )
        )
        assert session.get(Run, default_run).status == "succeeded"  # type: ignore[union-attr]
        # A detected write is a review-required finding, never a second approval flavour.
        assert not session.scalars(
            select(ApprovalRequest).where(ApprovalRequest.run_id == default_run)
        ).first()
    assert any(e.severity == "error" and "detected, not prevented" in e.summary for e in events)


def test_protected_write_with_cancel_policy_stops_the_run(ctx: AppContext, repo: Path) -> None:
    code = "import pathlib, time\npathlib.Path('.github/workflows/ci.yml').write_text('x\\n')\ntime.sleep(20)\n"
    run_id = run_agent(ctx, project(ctx, repo, cancel_on_protected_write=True), code)
    with ctx.session() as session:
        run = session.get(Run, run_id)
        assert run is not None
        assert run.failure_category == FailureCategory.POLICY_VIOLATION.value, run.failure_message
    assert summaries(ctx, run_id)[".github/workflows/ci.yml"].policy["cancelled"] is True  # type: ignore[index]


def test_outside_boundary_write_is_reported_and_stops_the_run(
    ctx: AppContext, repo: Path, client: TestClient
) -> None:
    outside = Path(ctx.settings.data_dir).parent / "outside-secret.txt"
    run_id = run_agent(
        ctx,
        project(ctx, repo),
        "import time\ntime.sleep(20)\n",
        records=[{"type": "file_write", "path": str(outside), "operation": "modify", "bytes": 3}],
    )
    with ctx.session() as session:
        run = session.get(Run, run_id)
        assert run is not None and run.failure_category == FailureCategory.POLICY_VIOLATION.value
        events = list(
            session.scalars(
                select(Event).where(Event.run_id == run_id, Event.type == "file_evidence")
            )
        )
    assert any(e.severity == "error" and "Outside-boundary" in e.summary for e in events)
    assert not outside.exists()  # AgentTape never touched it
    row = summaries(ctx, run_id)[str(outside)]
    assert row.classification == "outside_boundary" and row.is_outside
    exported = client.get(f"/api/v1/runs/{run_id}/evidence/export").text
    assert str(outside) not in exported and "<outside-boundary>/outside-secret.txt" in exported
    summary = client.get(f"/api/v1/runs/{run_id}/files/summary").json()
    assert summary["counts"]["outside_findings"] == 1


def test_git_change_missed_by_the_watcher_and_a_telemetry_read(
    ctx: AppContext, repo: Path, client: TestClient
) -> None:
    run_id = run_agent(
        ctx,
        project(ctx, repo, watcher=False),
        "import pathlib\npathlib.Path('src/app.py').write_text('print(2)\\n')\n",
        records=[{"type": "file_read", "path": "docs/guide.md", "bytes": 16}],
    )
    rows = summaries(ctx, run_id)
    changed = rows["src/app.py"]
    assert changed.classification == "modified"
    assert changed.completeness == "reconciled_only"
    assert set(changed.sources) == {"git_end", "reconciliation"}
    read = rows["docs/guide.md"]
    assert read.classification == "observed_read" and read.net_status == "unchanged"
    assert read.operation_counts == {"read": 1}
    summary = client.get(f"/api/v1/runs/{run_id}/files/summary").json()
    assert summary["completeness"]["writes"] == "unavailable"
    assert summary["completeness"]["statement"].startswith(
        "Filesystem observation unavailable; final Git reconciliation completed"
    )
    with ctx.session() as session:
        findings = session.scalars(
            select(FileEvidenceEvent).where(
                FileEvidenceEvent.run_id == run_id, FileEvidenceEvent.source == "reconciliation"
            )
        ).all()
        assert [f.details["finding"] for f in findings] == ["changed_without_watcher_event"]
        attempt = session.scalars(select(RunAttempt).where(RunAttempt.run_id == run_id)).one()
        count = session.scalar(
            select(func.count(FileEvidenceEvent.id)).where(FileEvidenceEvent.run_id == run_id)
        )
    # Reconciliation is idempotent: running it again (restart, retry) adds nothing.
    reconcile_run(
        ctx,
        run_id=run_id,
        attempt_id=attempt.id,
        root=Path(str(attempt.workdir)),
        noise=NoisePolicy(),
    )
    with ctx.session() as session:
        again = session.scalar(
            select(func.count(FileEvidenceEvent.id)).where(FileEvidenceEvent.run_id == run_id)
        )
        run = session.get(Run, run_id)
        assert run is not None
        status = store.completeness(session, run)
    assert again == count
    assert status["reads"] == "telemetry"

    # A later reconciliation supersedes the earlier one (a pause for approval reconciles
    # provisionally; the resumed attempt may revert the change).
    Path(str(attempt.workdir), "src", "app.py").write_text('print("hello")\n')
    reconcile_run(
        ctx,
        run_id=run_id,
        attempt_id=attempt.id,
        root=Path(str(attempt.workdir)),
        noise=NoisePolicy(),
    )
    with ctx.session() as session:
        stale = session.scalars(
            select(FileEvidenceEvent.source).where(
                FileEvidenceEvent.run_id == run_id, FileEvidenceEvent.path == "src/app.py"
            )
        ).all()
    assert stale == []


def test_reconnect_offline_window_marks_evidence_partial(
    ctx: AppContext, repo: Path, client: TestClient
) -> None:
    run_id = run_agent(ctx, project(ctx, repo), "pass")
    from agenttape.application.events import EventRecorder

    with ctx.session() as session:
        EventRecorder(ctx.settings, ctx.clock).record(
            session,
            run_id,
            "runtime",
            "Reconnected to Herdr",
            {
                "runtime": "herdr",
                "action": "reconnect",
                "offline_from": "2026-09-01T10:00:00+00:00",
                "offline_to": "2026-09-01T10:02:00+00:00",
            },
        )
    completeness = client.get(f"/api/v1/runs/{run_id}/files/summary").json()["completeness"]
    assert completeness["level"] == "partial"
    assert completeness["statement"].startswith(
        "Partial: AgentTape was offline from 2026-09-01T10:00:00+00:00"
    )


@pytest.mark.slow
def test_ten_thousand_filesystem_events_stay_bounded(
    ctx: AppContext, repo: Path, client: TestClient
) -> None:
    code = (
        "import pathlib\n"
        "for i in range(10000):\n"
        "    d = pathlib.Path('node_modules') / f'p{i // 500}'\n"
        "    d.mkdir(parents=True, exist_ok=True)\n"
        "    (d / f'f{i}.js').write_text('x')\n"
        "for i in range(20):\n"
        "    pathlib.Path(f'src/gen{i}.py').write_text(f'V = {i}\\n')\n"
    )
    run_id = run_agent(ctx, project(ctx, repo), code)
    with ctx.session() as session:
        rows = session.scalar(
            select(func.count(FileEvidenceEvent.id)).where(FileEvidenceEvent.run_id == run_id)
        )
    assert rows is not None and rows < 200  # 10,000 events never become 10,000 rows
    page = client.get(f"/api/v1/runs/{run_id}/files", params={"limit": 50}).json()
    summary = client.get(f"/api/v1/runs/{run_id}/files/summary").json()
    assert page["total"] == 21  # 20 source files + one collapsed node_modules/ row
    assert summary["counts"]["created"] == 20
    collapsed_count = assert_collapsed_bucket_consistent(ctx, run_id, "node_modules/")
    assert summary["counts"]["collapsed_events"] == collapsed_count
