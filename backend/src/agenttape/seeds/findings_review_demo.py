"""The demo's one on-demand challenge: a scripted reviewer, no model call.

Adds "Demo Agent (reviewer)" (adapter ``demo``, scenario ``finding_review``) and, when runs
are seeded and can execute inline, one real challenge of the primary evidence run
(``file_evidence_demo.RUN_TITLE``) through :meth:`FindingReviewService.start_challenge` -
the one path every challenge takes, on demand or from a workflow node. Its outcomes are
model judgment (``findings/review.py`` ``REVIEW_OUTCOMES``) and never touch a citation
verdict; this module never writes a verdict, a finding, or a review outcome directly - the
challenge is an ordinary child run, executed by the scripted reviewer adapter and settled
by the same in-process worker every other seeded demo run uses.

"Demo Agent (reviewer)" is deliberately not added to ``core_demo.DEMO_PROFILES``: that dict
doubles as the list of scenarios "Start a demo run" can launch, and a reviewer started on
its own has no target run's findings to review.

Runs after ``findings_demo`` (``ORDER = 80``) so the evidence run's engine verdicts are
already settled before the reviewer reads them.
"""

from __future__ import annotations

from sqlalchemy import select

from agenttape.application.context import AppContext
from agenttape.application.schemas.catalog import AgentProfileCreate
from agenttape.application.schemas.finding_review import ChallengeCreate
from agenttape.application.services.finding_review import FindingReviewService
from agenttape.application.services.profiles import AgentProfileService
from agenttape.domain.enums import TERMINAL_STATUS_VALUES
from agenttape.infrastructure.db.models import AgentProfile, Run
from agenttape.seeds.base import SeedReport
from agenttape.seeds.file_evidence_demo import RUN_TITLE
from agenttape.seeds.story_runs import _can_execute_inline, settle_demo_runs

ORDER = 85

REVIEWER_NAME = "Demo Agent (reviewer)"
REVIEWER_DESCRIPTION = (
    "Scripted reviewer for Challenge findings: re-reads cited files in its own copy of the "
    "demo repository. No model; its outcomes are labelled model judgment."
)


def _reviewer_profile(ctx: AppContext, report: SeedReport) -> str:
    with ctx.session() as session:
        existing = session.scalars(
            select(AgentProfile).where(AgentProfile.name == REVIEWER_NAME)
        ).first()
        if existing is not None:
            report.add("agent_profile", REVIEWER_NAME, created=False)
            return existing.id
    created = AgentProfileService(ctx).create(
        AgentProfileCreate(
            name=REVIEWER_NAME,
            adapter_type="demo",
            description=REVIEWER_DESCRIPTION,
            tags=["demo", "review"],
            config={"scenario": "finding_review"},
        ),
        actor="demo seed",
        is_demo=True,
    )
    report.add("agent_profile", REVIEWER_NAME, created=True)
    return created.id


def seed(ctx: AppContext, report: SeedReport) -> None:
    profile_id = _reviewer_profile(ctx, report)
    run_ids: list[str] = report.data.get("evidence_runs") or []
    if not report.include_runs or not run_ids:
        return
    if not (report.execute_inline and _can_execute_inline()):
        return  # HTTP seed only queues the evidence runs; nothing to challenge yet.

    # file_evidence_demo appends the primary evidence run (RUN_TITLE, variant "a") first.
    target_run_id = run_ids[0]
    with ctx.session() as session:
        target = session.get(Run, target_run_id)
        if target is None or target.status not in TERMINAL_STATUS_VALUES:
            report.add("challenge", RUN_TITLE, created=False)
            return

    service = FindingReviewService(ctx)
    if service.challenges(target_run_id):
        report.add("challenge", RUN_TITLE, created=False)
        return

    service.start_challenge(
        target_run_id,
        ChallengeCreate(agent_profile_id=profile_id, title=f"Challenge: {RUN_TITLE}"),
        actor="demo seed",
    )
    settle_demo_runs(ctx)
    report.add("challenge", RUN_TITLE, created=True)
