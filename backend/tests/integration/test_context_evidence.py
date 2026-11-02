"""Context evidence through the real worker (M2).

A scripted adapter emits events of exactly the shape the evidence demo agent produces
after normalization (``context``, ``finding``, ``file_read``, MCP ``tool_call`` /
``tool_result``) and records the prompt it was handed, so the tests prove the pack, the
normalizer, the comparison, and the reviewed learning loop end to end.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from agenttape.adapters.base import (
    AdapterResult,
    AgentAdapter,
    DiscoveryResult,
    ExecutionContext,
    ProfileIssue,
)
from agenttape.application.context import AppContext
from agenttape.application.schemas.catalog import AgentProfileCreate, ProjectCreate
from agenttape.application.schemas.runs import RunCreate
from agenttape.application.services.profiles import AgentProfileService
from agenttape.application.services.projects import ProjectService
from agenttape.application.services.runs import RunService
from agenttape.context_evidence.export import context_export
from agenttape.context_evidence.hooks import backfill_findings
from agenttape.domain.enums import AdapterAvailability, EventType
from agenttape.infrastructure.db.extra_models.context_evidence import (
    ContextEvidenceEvent,
    ContextFeedback,
    ContextPack,
    ContextSource,
    EvidenceLink,
)
from agenttape.infrastructure.db.extra_models.findings import Finding
from agenttape.worker.worker import Worker
from tests.conftest import drive
from tests.integration.helpers import git

#: A fake credential, assembled at runtime so the secret scan does not flag the test.
LEAK = "-".join(["sk", "live", "hunter2", "0123456789abcdef"])


class ScriptedAdapter(AgentAdapter):
    """Emits ``config.records`` as events and remembers the prompt it received."""

    name = "test_context"
    display_name = "Scripted context agent"
    prompts: ClassVar[list[str | None]] = []

    def discover(self) -> DiscoveryResult:
        return DiscoveryResult(adapter=self.name, status=AdapterAvailability.READY, version="1")

    def validate_profile(self, config: dict[str, Any]) -> list[ProfileIssue]:
        return []

    async def execute(self, ctx: ExecutionContext) -> AdapterResult:
        type(self).prompts.append(ctx.resolved_prompt())
        ids: dict[str, str] = {}
        for rec in ctx.profile.get("records", []):
            etype = EventType(rec["type"])
            payload = dict(rec.get("payload") or {})
            parent = (
                ids.get(str(payload.get("tool_use_id"))) if etype == EventType.TOOL_RESULT else None
            )
            from agenttape.infrastructure.db.base import new_id

            eid = new_id()
            if etype == EventType.TOOL_CALL:
                ids[str(payload.get("tool_use_id"))] = eid
            await ctx.events.event(
                etype, rec.get("summary", etype.value), payload, id=eid, parent_event_id=parent
            )
        return AdapterResult.success(exit_code=0)


@pytest.fixture
def project(ctx: AppContext) -> str:
    repo = Path(ctx.settings.allowed_roots[0]) / "svc"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "greeter.py").write_text("def greet():\n    return 'hi'\n")
    (repo / "CLAUDE.md").write_text("# Rules\nKeep functions small.\n")
    (repo / "AGENTS.md").write_text("# Rules\nKeep functions small.\n")
    git(repo, "init", "--quiet", "--initial-branch=main")
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", "init")
    return ProjectService(ctx).create(ProjectCreate(name="Svc", repo_path=str(repo))).id


def ctx_rec(stage: str, sid: str, *, live: bool = True, **extra: Any) -> dict[str, Any]:
    return {
        "type": "context",
        "payload": {
            "stage": stage,
            "source": {
                "id": sid,
                "type": extra.pop("type", "knowledge_doc"),
                "name": extra.pop("name", sid),
                "provider": "fixture-kb",
                "locator": extra.pop("locator", f"kb/{sid}"),
                "authority": extra.pop("authority", "reference"),
                "version": extra.pop("version", None),
                "synced_at": extra.pop("synced_at", None),
                "live": live,
            },
            "query": extra.pop("query", None),
            "tool": extra.pop("tool", None),
            "result": extra.pop("result", None),
            "reason": extra.pop("reason", None),
        },
    }


def run_with(
    ctx: AppContext, project: str, records: list[dict[str, Any]], prompt: str = "Fix src/greeter.py"
) -> str:
    if not ctx.adapters.has("test_context"):
        ctx.adapters.register(ScriptedAdapter())
    profile = AgentProfileService(ctx).create(
        AgentProfileCreate(
            name=f"scripted-{len(ScriptedAdapter.prompts)}-{abs(hash(json.dumps(records))) % 10_000}",
            adapter_type="test_context",
            config={"records": records, "model": "fixture-model"},
        )
    )
    run = RunService(ctx).create_run(
        RunCreate(agent_profile_id=profile.id, project_id=project, prompt=prompt)
    )
    detail = drive(ctx, run.run.id)
    assert detail.run.status == "succeeded", detail.run.failure_message
    return run.run.id


def evidence(ctx: AppContext, run_id: str) -> list[ContextEvidenceEvent]:
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


def test_pack_selects_compact_index_before_targeted_files(ctx: AppContext, project: str) -> None:
    run_id = run_with(ctx, project, [], prompt="Fix src/greeter.py; see docs/missing.md")
    with ctx.session() as session:
        pack = session.scalars(select(ContextPack).where(ContextPack.run_id == run_id)).one()
        items, excluded = list(pack.items), list(pack.excluded)
    keys = [i["source_key"] for i in items]
    assert keys.index("index:git-ls-files") < keys.index("file:src/greeter.py")
    assert {"file:CLAUDE.md", "file:AGENTS.md"} <= set(keys)
    claude = next(i for i in items if i["source_key"] == "file:CLAUDE.md")
    assert claude["stage"] == "selected" and claude["excerpt"] is None
    assert len(claude["content_hash"]) == 64
    assert any(e["name"] == "docs/missing.md" for e in excluded)
    profile = next(i for i in items if i["kind"] == "agent_profile")
    assert profile["details"]["settings"]["model"] == "fixture-model"


def test_snapshot_and_live_sources_stay_distinguishable(
    ctx: AppContext, project: str, client: TestClient
) -> None:
    run_id = run_with(
        ctx,
        project,
        [
            ctx_rec(
                "retrieved",
                "kb:glossary",
                live=False,
                authority="snapshot",
                synced_at="2026-09-01T00:00:00Z",
                version="idx-42",
            ),
            ctx_rec(
                "retrieved",
                "dw:orders",
                live=True,
                authority="authoritative",
                type="warehouse_table",
            ),
        ],
    )
    body = client.get(
        f"/api/v1/runs/{run_id}/context/evidence", params={"freshness": "snapshot"}
    ).json()
    [snap] = [r for r in body["items"] if r["source_key"] == "kb:glossary"]
    assert snap["synced_at"] == "2026-09-01T00:00:00Z" and snap["version"] == "idx-42"
    live = client.get(
        f"/api/v1/runs/{run_id}/context/evidence", params={"freshness": "live"}
    ).json()
    assert "dw:orders" in {r["source_key"] for r in live["items"]}
    assert "kb:glossary" not in {r["source_key"] for r in live["items"]}
    summary = client.get(f"/api/v1/runs/{run_id}/context").json()
    assert summary["snapshot_sources"] >= 1 and summary["live_sources"] >= 1


def test_mcp_result_is_sanitized_and_no_secret_is_persisted(
    ctx: AppContext, project: str, client: TestClient
) -> None:
    rows = [{"id": i, "total": i * 3} for i in range(5)]
    run_id = run_with(
        ctx,
        project,
        [
            {
                "type": "tool_call",
                "payload": {
                    "tool": "mcp__warehouse__run_query",
                    "input": {
                        "sql": "select id, total from orders limit 5",
                        "connection": f"postgres://svc:{LEAK}@dw.internal/prod",
                        "password": LEAK,
                    },
                    "tool_use_id": "q1",
                },
            },
            {
                "type": "tool_result",
                "payload": {
                    "tool_use_id": "q1",
                    "output": json.dumps(rows),
                    "is_error": False,
                    "truncated": True,
                },
            },
            {
                "type": "tool_call",
                "payload": {
                    "tool": "mcp__jira__get_issue",
                    "input": {"key": "ABC-1"},
                    "tool_use_id": "q2",
                },
            },
            {
                "type": "tool_result",
                "payload": {"tool_use_id": "q2", "output": "permission denied", "is_error": True},
            },
        ],
    )
    mcp = [r for r in evidence(ctx, run_id) if r.source_type == "mcp_tool"]
    ok = next(r for r in mcp if r.source_key == "mcp:warehouse/run_query")
    assert ok.stage == "retrieved" and ok.result is not None and ok.tool is not None
    assert ok.result["rows"] == 5 and ok.result["schema"] == ["id", "total"]
    assert ok.result["truncated"] is True and len(ok.result["hash"]) == 64
    assert ok.tool["args"]["sql"] == "select id, total from orders limit 5"
    assert ok.tool["started_at"] <= ok.tool["ended_at"]
    denied = next(r for r in mcp if r.source_key == "mcp:jira/get_issue")
    assert denied.stage == "unavailable" and denied.tool and denied.tool["status"] == "denied"
    # the secret appears nowhere in context evidence, sources, or the export
    with ctx.session() as session:
        dump = json.dumps(
            [
                [r.tool, r.result, r.details, r.reason, r.query]
                for r in session.scalars(select(ContextEvidenceEvent))
            ]
            + [[s.locator, s.name] for s in session.scalars(select(ContextSource))],
            default=str,
        )
    assert LEAK not in dump
    with ctx.session() as session:
        export = json.dumps(context_export(session, run_id), default=str)
    assert LEAK not in export
    assert '"total": 3' not in export  # result rows themselves are never stored


def test_retrieved_source_is_not_labelled_injected_or_cited(
    ctx: AppContext, project: str, client: TestClient
) -> None:
    run_id = run_with(
        ctx, project, [{"type": "file_read", "payload": {"path": "src/greeter.py", "bytes": 30}}]
    )
    agent_rows = [r for r in evidence(ctx, run_id) if r.event_id]
    assert [(r.source_key, r.stage) for r in agent_rows] == [("file:src/greeter.py", "retrieved")]
    body = client.get(f"/api/v1/runs/{run_id}/context/evidence", params={"q": "greeter.py"}).json()
    stages = {r["stage"] for r in body["items"]}
    assert "cited" not in stages and "injected" not in stages
    assert all(not r["cited_in_run"] for r in body["items"])
    detail = client.get(f"/api/v1/runs/{run_id}/context/sources/{agent_rows[0].source_id}").json()
    assert detail["file_path"] == "src/greeter.py"
    assert [a["stage"] for a in detail["access"]] == ["selected", "retrieved"]


def test_two_runs_with_different_context_compare_meaningfully(
    ctx: AppContext, project: str, client: TestClient
) -> None:
    a = run_with(
        ctx,
        project,
        [
            ctx_rec("retrieved", "kb:pricing", live=False, version="v1", query="pricing rules"),
            ctx_rec("unavailable", "mcp:dw", reason="DW MCP server not configured"),
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
    b = run_with(
        ctx,
        project,
        [
            ctx_rec("retrieved", "kb:pricing", live=False, version="v2", query="pricing rules"),
            ctx_rec("retrieved", "dw:prices", live=True, query="select price"),
            {
                "type": "finding",
                "payload": {
                    "id": "F1",
                    "text": "Price is 12",
                    "links": [{"source_id": "dw:prices", "relation": "supports"}],
                },
            },
        ],
        prompt="Fix src/greeter.py pricing",
    )
    cmp = client.get(f"/api/v1/runs/{a}/context/compare/{b}").json()
    changes = {s["key"]: s["change"] for s in cmp["sources"]}
    assert changes["dw:prices"] == "added"
    assert changes["mcp:dw"] == "removed"
    assert any(
        v["key"] == "kb:pricing" and v["a"]["version"] == "v1" and v["b"]["version"] == "v2"
        for v in cmp["versions"]
    )
    assert cmp["queries"]["b_only"] == ["select price"]
    assert cmp["gaps"]["resolved"] and not cmp["gaps"]["introduced"]
    assert cmp["citations"] == {"a_only": ["kb:pricing"], "b_only": ["dw:prices"]}
    assert cmp["conclusions"] == {"a_only": ["Price is 10"], "b_only": ["Price is 12"]}
    assert cmp["prompt"]["same"] is False


def test_correction_stays_with_its_run_until_a_person_approves_it(
    ctx: AppContext, project: str, client: TestClient
) -> None:
    lesson = "Totals are reported in cents, not dollars."
    a = run_with(
        ctx,
        project,
        [
            {
                "type": "context",
                "payload": {
                    "stage": "correction",
                    "text": lesson,
                    "category": "business-knowledge",
                },
            }
        ],
    )
    [fb] = client.get("/api/v1/context/feedback", params={"run_id": a}).json()
    assert fb["status"] == "pending" and fb["run_id"] == a

    b = run_with(ctx, project, [])  # nothing approved yet: nothing is injected
    assert lesson not in (ScriptedAdapter.prompts[-1] or "")
    with ctx.session() as session:
        pack_b = session.scalars(select(ContextPack).where(ContextPack.run_id == b)).one()
        assert not any(i["kind"] == "lesson" for i in pack_b.items)

    r = client.post(
        f"/api/v1/context/feedback/{fb['id']}/classify",
        json={"classification": "business-knowledge"},
    )
    assert r.status_code == 200 and r.json()["proposed_destination"] == "project_instruction"
    r = client.post(
        f"/api/v1/context/feedback/{fb['id']}/approve", json={"note": "confirmed with finance"}
    )
    assert r.status_code == 200, r.text
    approved = r.json()
    assert approved["lesson_version"] == 1 and approved["run_id"] == a

    c = run_with(ctx, project, [])
    assert lesson in (ScriptedAdapter.prompts[-1] or "")  # actually handed to the agent
    packs = client.get(f"/api/v1/runs/{c}/context/packs").json()
    [item] = [i for i in packs[0]["items"] if i["kind"] == "lesson"]
    assert item["stage"] == "injected" and item["version"] == "v1"
    assert item["details"]["origin_run_id"] == a
    assert packs[0]["injected_text"] and lesson in packs[0]["injected_text"]
    activity = client.get(f"/api/v1/projects/{project}/context/activity").json()
    [row] = activity["lessons"]
    assert row["run_id"] == a and row["injected_runs"] == 1
    with ctx.session() as session:
        assert session.get(ContextFeedback, fb["id"]).status == "approved"  # type: ignore[union-attr]
    # approving twice is refused
    assert client.post(f"/api/v1/context/feedback/{fb['id']}/approve").status_code == 409


def test_a_gap_resolved_on_a_source_that_is_still_missing_elsewhere_is_reported(
    ctx: AppContext, project: str, client: TestClient
) -> None:
    missing_wiki = {
        "type": "finding",
        "payload": {
            "id": "F1",
            "text": "Keep informal greetings",
            "links": [{"source_id": "wiki:style", "relation": "missing"}],
        },
    }
    a = run_with(
        ctx,
        project,
        [
            ctx_rec("gap", "wiki:style", reason="No documented owner for localized greetings"),
            missing_wiki,
        ],
    )
    b = run_with(ctx, project, [missing_wiki])
    cmp = client.get(f"/api/v1/runs/{a}/context/compare/{b}").json()
    assert cmp["gaps"] == {
        "resolved": ["gap: No documented owner for localized greetings"],
        "introduced": [],
    }


def test_findings_recorded_before_the_findings_table_are_back_filled(
    ctx: AppContext, project: str, client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """On an upgraded database a finished run can have FINDING events (and legacy
    source links) but no ``findings`` rows, since ``m3F_0001`` created the table with no
    back-fill. ``backfill_findings`` ingests exactly those events through the one
    normalizer path, so the Findings tab shows them again; it is idempotent, and the
    worker runs it when it starts."""
    run_id = run_with(
        ctx,
        project,
        [
            ctx_rec("retrieved", "kb:pricing", live=False, version="v1", query="pricing rules"),
            {
                "type": "finding",
                "payload": {
                    "id": "F1",
                    "text": "Price is 10",
                    "links": [{"source_id": "kb:pricing", "relation": "supports"}],
                },
            },
            {"type": "finding", "payload": {"id": "F2", "text": "No links at all"}},
        ],
    )
    url = f"/api/v1/runs/{run_id}/context/findings"
    before = client.get(url).json()
    assert sorted(f["key"] for f in before) == ["F1", "F2"]

    # What a pre-upgrade run looks like: the events and legacy links, no findings rows.
    with ctx.session() as session:
        session.execute(delete(Finding).where(Finding.run_id == run_id))
    caplog.set_level(logging.DEBUG, logger="agenttape.context_evidence.service")
    assert client.get(url).json() == []
    client.get(url)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    assert backfill_findings(ctx) == 2
    after = client.get(url).json()
    assert sorted(f["key"] for f in after) == ["F1", "F2"]
    [f1] = [f for f in after if f["key"] == "F1"]
    assert [s["key"] for s in f1["supports"]] == ["kb:pricing"]
    with ctx.session() as session:
        links = session.scalars(select(EvidenceLink).where(EvidenceLink.run_id == run_id)).all()
        assert len(links) == 1, "the back-fill never duplicates a legacy link"

    assert backfill_findings(ctx) == 0

    # The worker runs the back-fill once when it starts.
    with ctx.session() as session:
        session.execute(delete(Finding).where(Finding.run_id == run_id))
    stop = asyncio.Event()
    stop.set()
    asyncio.run(Worker(ctx, worker_id="backfill-on-start", concurrency=1).run(stop))
    assert sorted(f["key"] for f in client.get(url).json()) == ["F1", "F2"]
