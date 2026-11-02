"""The demo story `agenttape seed` promises, verified against a fresh seed."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from agenttape.application.context import AppContext
from agenttape.application.services.demo import DemoService
from agenttape.application.services.finding_review import FindingReviewService
from agenttape.application.services.findings import FindingsService
from agenttape.application.services.runs import RunService
from agenttape.demo_agent import DECLARED_ONLY_FINDINGS, DESIGNED_VERDICTS
from agenttape.findings.citations import CITATION_VERDICTS
from agenttape.infrastructure.db.extra_models.findings import CitationVerdict, Finding
from agenttape.infrastructure.db.models import (
    AgentProfile,
    Event,
    Project,
    PromptTemplate,
    PromptVersion,
    Run,
    Schedule,
    Workflow,
    WorkflowNode,
    WorkflowVersion,
)
from agenttape.seeds.core_demo import DEMO_PROFILES
from agenttape.seeds.file_evidence_demo import RUN_TITLE, RUN_TITLE_B
from agenttape.seeds.findings_demo import STAND_INS
from agenttape.seeds.findings_review_demo import REVIEWER_NAME
from tests.integration.helpers import run_findings


def test_fresh_seed_tells_the_whole_story(ctx: AppContext) -> None:
    DemoService(ctx).seed()
    with ctx.session() as s:
        assert (
            s.scalars(select(Project).where(Project.name == "AgentTape Demo Project")).one().is_demo
        )
        profiles = {
            p.name for p in s.scalars(select(AgentProfile).where(AgentProfile.is_demo.is_(True)))
        }
        assert {name for name, _d, _c in DEMO_PROFILES.values()} <= profiles
        with_vars = [
            v
            for v in s.scalars(
                select(PromptVersion).join(PromptTemplate).where(PromptTemplate.is_demo.is_(True))
            )
            if v.variables
        ]
        assert len(with_vars) >= 2
        nightly = s.scalars(select(Schedule).where(Schedule.is_demo.is_(True))).one()
        assert "Nightly" in nightly.name and nightly.enabled is False
        workflow = s.scalars(select(Workflow).where(Workflow.name == "Safe Code Change")).one()
        version = s.scalars(
            select(WorkflowVersion).where(WorkflowVersion.workflow_id == workflow.id)
        ).first()
        assert version is not None
        node_types = {
            n.node_type
            for n in s.scalars(
                select(WorkflowNode).where(WorkflowNode.workflow_version_id == version.id)
            )
        }
        assert {"command", "agent_run", "approval", "evaluation_gate"} <= node_types
        runs = s.scalars(select(Run).where(Run.is_demo.is_(True))).all()
        by_status: dict[str, list[Run]] = {}
        for r in runs:
            by_status.setdefault(r.status, []).append(r)
        events = dict(s.execute(select(Event.run_id, func.count()).group_by(Event.run_id)).all())
    succeeded = by_status.get("succeeded", [])
    assert len(succeeded) >= 3 and all(events[r.id] >= 10 for r in succeeded)
    assert len(by_status.get("waiting_for_approval", [])) == 1
    assert any(r.failure_category == "validation_failed" for r in by_status.get("failed", []))
    assert by_status.get("timed_out"), "a run stopped by its wall-clock limit"
    assert any(r.attempt_count == 2 for r in succeeded), "a run that recovered by automatic retry"
    forks = [r for r in runs if r.parent_relation == "fork"]
    assert forks and forks[0].parent_run_id in {r.id for r in runs}

    # The demo and the findings service share one verdict vocabulary.
    assert set(DESIGNED_VERDICTS.values()) == set(CITATION_VERDICTS)

    evidence_run_ids = {r.title: r.id for r in runs if r.title in (RUN_TITLE, RUN_TITLE_B)}
    assert set(evidence_run_ids) == {RUN_TITLE, RUN_TITLE_B}
    stand_in_keys = {f"{key}#{position}" for key, position in STAND_INS}

    def evidence_checks(run_id: str) -> tuple[dict[str, int], dict[str, int]]:
        """Every citation's check/reproduction count and latest verdict, keyed by
        (finding key, position); also asserts the finding/citation invariants a fresh seed
        must hold.

        Every latest verdict is the real engine's own (``method="lexical_v1"``) except a
        citation named by ``STAND_INS``, whose latest row is the one labelled
        ``method="seeded"`` stand-in, chained (``rerun_of_id``) to the engine's own verdict
        one row back in the history. F2#1 (a citation of a real, recorded ``git grep``) is
        also proven reproduced by AgentTape's own automatic re-run, never seeded.
        """
        findings = run_findings(ctx, run_id)
        declared_only_keys = [f["key"] for f in findings if f["declared_only"]]
        assert declared_only_keys == list(DECLARED_ONLY_FINDINGS)
        verdicts_seen: set[str] = set()
        checks: dict[str, int] = {}
        reproductions: dict[str, int] = {}
        stand_in_rows = 0
        for finding in findings:
            for citation in finding["citations"]:
                key = f"{finding['key']}#{citation['position']}"
                designed = DESIGNED_VERDICTS[(finding["key"], citation["position"])]
                verdict = citation["verdict"]
                assert verdict is not None, f"{key} has no latest verdict"
                verdicts_seen.add(verdict["verdict"])
                checks[key] = citation["checks"]
                reproductions[key] = citation["reproductions"]
                stand_in_verdict = STAND_INS.get((finding["key"], citation["position"]))
                if stand_in_verdict is not None:
                    stand_in_rows += 1
                    assert verdict["method"] == "seeded"
                    assert verdict["verdict"] == stand_in_verdict
                    assert verdict["reason"].startswith("Seeded demo stand-in")
                    with ctx.session() as session:
                        engine_row = session.get(CitationVerdict, verdict["rerun_of_id"])
                        assert engine_row is not None
                        assert engine_row.method == "lexical_v1"
                        assert engine_row.verdict == "never_read"
                else:
                    assert verdict["method"] == "lexical_v1"
                    assert verdict["verdict"] == designed
                if finding["key"] == "F3" and citation["position"] == 1:
                    assert citation["event_id"] is not None
                if finding["key"] == "F2" and citation["position"] == 1:
                    # A real search, cited by an event citation: AgentTape's own automatic
                    # re-run must have reproduced it - never a seeded row.
                    assert citation["event_id"] is not None
                    reproduction = citation["reproduction"]
                    assert reproduction is not None, f"{key} has no reproduction"
                    assert reproduction["trigger"] == "automatic"
                    assert reproduction["mode"] == "search"
                    assert reproduction["status"] == "done"
                    assert reproduction["result"] == "reproduced", reproduction["reason"]
                    assert reproduction["pinned_commit"]
        assert verdicts_seen == set(CITATION_VERDICTS)
        assert stand_in_rows == len(STAND_INS)
        return checks, reproductions

    before = {title: evidence_checks(rid) for title, rid in evidence_run_ids.items()}
    checks_before = {title: checks for title, (checks, _reproductions) in before.items()}
    reproductions_before = {
        title: reproductions for title, (_checks, reproductions) in before.items()
    }
    for title, checks in checks_before.items():
        for key, count in checks.items():
            assert count == (2 if key in stand_in_keys else 1), (title, key)

    # Re-seeding is a no-op: no new records, and no new (or changed) verdict or
    # reproduction rows.
    again = DemoService(ctx).seed()
    assert not again.created
    assert RunService(ctx).list_runs(is_demo=True).total == len(runs)
    for title, run_id in evidence_run_ids.items():
        checks_after, reproductions_after = evidence_checks(run_id)
        assert checks_after == checks_before[title]
        assert reproductions_after == reproductions_before[title]

    # The seeded challenge: a real, scripted-reviewer child run of the primary
    # evidence run, created only through FindingReviewService - never a hand-inserted
    # review row.
    with ctx.session() as s:
        reviewer = s.scalars(select(AgentProfile).where(AgentProfile.name == REVIEWER_NAME)).one()
        assert reviewer.adapter_type == "demo"
        assert reviewer.is_demo is True

    primary_run_id = evidence_run_ids[RUN_TITLE]
    challenges = FindingReviewService(ctx).challenges(primary_run_id)
    assert len(challenges) == 1, "reseeding must not start a second challenge"
    challenge = challenges[0]
    assert challenge.trigger == "on_demand"
    assert challenge.state == "completed"
    assert challenge.outcomes, "the scripted reviewer must record at least one outcome"
    assert challenge.judgment == "model judgment"
    assert challenge.counts_as_proof is False
    assert challenge.run.is_demo is True
    assert challenge.run.title == f"Challenge: {RUN_TITLE}"

    # The challenge never changes the target's own citation verdicts: checks_after (taken
    # after the reseed, which also re-drives the challenge's own idempotency check) still
    # matches checks_before for every citation on the challenged run.
    assert checks_before[RUN_TITLE] == evidence_checks(primary_run_id)[0]

    # The dashboard's findings block and GET /findings share one cross-run read
    # model. Recompute the tally directly from findings_for_run of every run with findings
    # (every seed run settles within this test, well inside a 7-day window) and prove it
    # equals window_tally's own count, using the documented definitions.
    with ctx.session() as s:
        run_ids_with_findings = sorted(set(s.scalars(select(Finding.run_id).distinct())))
    recorded = declared_only = all_quotes_match = citations_total = unchecked = 0
    verdict_totals: dict[str, int] = {}
    for run_id in run_ids_with_findings:
        for finding in run_findings(ctx, run_id):
            recorded += 1
            cites = finding["citations"]
            citations_total += len(cites)
            if not cites:
                declared_only += 1
                continue
            citation_verdicts: list[str] = []
            for citation in cites:
                verdict = citation["verdict"]
                key = verdict["verdict"] if verdict is not None else "unchecked"
                citation_verdicts.append(key)
                if key == "unchecked":
                    unchecked += 1
                else:
                    verdict_totals[key] = verdict_totals.get(key, 0) + 1
            if all(v == "quote_matches" for v in citation_verdicts):
                all_quotes_match += 1

    tally = FindingsService(ctx).window_tally(7)
    assert tally.recorded == recorded
    assert tally.declared_only == declared_only
    assert tally.all_quotes_match == all_quotes_match
    assert tally.citations == citations_total
    assert tally.unchecked == unchecked
    assert tally.verdicts == verdict_totals


def test_seed_fails_loudly_when_demo_runs_do_not_settle(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the in-process worker cannot finish the story runs in time, seeding raises a
    clear error listing them instead of leaving a half-finished demo silently."""
    from agenttape.seeds import story_runs
    from agenttape.worker.worker import Worker

    async def stalled(self: Worker, *, max_items: int = 100) -> int:
        return 0  # a worker that never makes progress

    monkeypatch.setattr(story_runs, "SEED_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(Worker, "run_until_idle", stalled)
    with pytest.raises(story_runs.SeedIncompleteError) as info:
        DemoService(ctx).seed()
    assert info.value.details["pending"]
    assert all(p["status"] == "queued" for p in info.value.details["pending"])
