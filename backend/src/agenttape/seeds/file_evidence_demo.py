"""M2 demo data: "Refactor with Full File Evidence" and its context-variant twin.

* Project "AgentTape Evidence Demo": the AgentTape-owned fixture repository with Git
  isolation (each run gets its own worktree, which is the file evidence boundary), the
  protected paths ``.github/**``, ``*.lock``, ``CHANGELOG.md``, and one validation command.
* Profiles "Demo Agent (full evidence)" (variant a) and "... variant b" (a fresher
  knowledge source; the context gap resolved) run the evidence demo agent
  (``python -m agenttape.demo_agent``) through the ``full_evidence`` scenario.
* Runs "Refactor with Full File Evidence" and "Refactor with Full File Evidence
  (variant b)": executed for real, paused on the protected-path approval, approved by the
  seed (as "demo seed"), resumed, and evaluated by a three-evaluator suite.

Runs after the story seed (order 70) so its settle step does not race this one. Inside a
running event loop (the HTTP seed endpoint) the runs are only queued; a worker executes
them and they wait for a human approval.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from agenttape.adapters.builtin._demo_repo import ensure_fixture_repo
from agenttape.application.context import AppContext
from agenttape.application.schemas.catalog import (
    AgentProfileCreate,
    ProjectCreate,
    ProjectSettings,
)
from agenttape.application.schemas.runs import RunCreate
from agenttape.application.services.approvals import ApprovalService
from agenttape.application.services.profiles import AgentProfileService
from agenttape.application.services.projects import ProjectService
from agenttape.application.services.runs import RunService
from agenttape.domain.enums import ApprovalStatus, RunSource
from agenttape.infrastructure.db.models import AgentProfile, ApprovalRequest, Project, Run
from agenttape.seeds.base import SeedReport
from agenttape.seeds.core_demo import IMPROVE_PROMPT
from agenttape.seeds.story_runs import _can_execute_inline, settle_demo_runs

ORDER = 75

EVIDENCE_PROJECT = "AgentTape Evidence Demo"
RUN_TITLE = "Refactor with Full File Evidence"
RUN_TITLE_B = "Refactor with Full File Evidence (variant b)"
GOAL = (
    "Refactor greet() to strip whitespace, move punctuation into a helper module, and wire "
    "the formatter into CI."
)
EVALUATION_SUITE: list[dict[str, Any]] = [
    {"type": "exit_code", "name": "Exit code is 0", "required": True, "config": {"expected": [0]}},
    {
        "type": "git_diff",
        "name": "Diff stays small",
        "required": False,
        "config": {"max_files": 10, "max_lines": 200, "protected_is_failure": False},
    },
    {
        "type": "duration",
        "name": "Finishes within 5 minutes",
        "required": False,
        "config": {"max_seconds": 300},
    },
]
#: variant -> (profile name, description)
PROFILES: dict[str, tuple[str, str]] = {
    "a": (
        "Demo Agent (full evidence)",
        "Runs the evidence demo agent: real reads, edits, a restore, a rename, a formatter, "
        "noisy installs, context records, and a protected-path approval.",
    ),
    "b": (
        "Demo Agent (full evidence, variant b)",
        "The same file work with different context: a fresher knowledge source and the "
        "context gap resolved (for run comparison).",
    ),
}


def _project(ctx: AppContext, report: SeedReport) -> str:
    with ctx.session() as session:
        existing = session.scalars(select(Project).where(Project.name == EVIDENCE_PROJECT)).first()
        if existing is not None:
            report.add("project", EVIDENCE_PROJECT, created=False)
            return existing.id
    repo = ensure_fixture_repo(ctx.settings.data_dir)
    created = ProjectService(ctx).create(
        ProjectCreate(
            name=EVIDENCE_PROJECT,
            repo_path=str(repo),
            description=(
                "The demo repository with Git isolation and File Evidence: every run's "
                "worktree is observed live and reconciled against Git at the end."
            ),
            tags=["demo", "evidence"],
            settings=ProjectSettings(
                timeout_seconds=300,
                git_isolation=True,
                protected_paths=[".github/**", "*.lock", "CHANGELOG.md"],
                validation_commands=[["python3", "-m", "compileall", "-q", "src"]],
            ),
        ),
        actor="demo seed",
        is_demo=True,
    )
    report.add("project", EVIDENCE_PROJECT, created=True)
    return created.id


def _profiles(ctx: AppContext, report: SeedReport, project_id: str) -> dict[str, str]:
    ids: dict[str, str] = {}
    service = AgentProfileService(ctx)
    for variant, (name, description) in PROFILES.items():
        with ctx.session() as session:
            existing = session.scalars(
                select(AgentProfile).where(AgentProfile.name == name)
            ).first()
            existing_id = existing.id if existing else None
        if existing_id:
            ids[variant] = existing_id
            report.add("agent_profile", name, created=False)
            continue
        ids[variant] = service.create(
            AgentProfileCreate(
                name=name,
                adapter_type="demo",
                description=description,
                project_id=project_id,
                tags=["demo", "evidence"],
                config={
                    "scenario": "full_evidence",
                    "variant": variant,
                    "evaluators": EVALUATION_SUITE,
                },
            ),
            actor="demo seed",
            is_demo=True,
        ).id
        report.add("agent_profile", name, created=True)
    return ids


def approve_evidence_runs(ctx: AppContext, run_ids: list[str]) -> int:
    """Approve the demo agent's protected-path request on the given runs (demo policy)."""
    with ctx.session() as session:
        pending = session.scalars(
            select(ApprovalRequest.id).where(
                ApprovalRequest.run_id.in_(run_ids),
                ApprovalRequest.status == ApprovalStatus.PENDING.value,
                ApprovalRequest.type == "protected_path",
            )
        ).all()
    service = ApprovalService(ctx)
    for approval_id in pending:
        service.approve(
            approval_id,
            reviewer="demo seed",
            note="Demo policy: CI workflow changes proposed by the demo agent are approved.",
        )
    return len(pending)


def seed(ctx: AppContext, report: SeedReport) -> None:
    project_id = _project(ctx, report)
    report.data["evidence_project_id"] = project_id
    profiles = _profiles(ctx, report, project_id)
    prompt_id = (report.data.get("prompts") or {}).get(IMPROVE_PROMPT)
    if not report.include_runs or prompt_id is None:
        return
    runs = RunService(ctx)
    ids: list[str] = []
    for variant, title in (("a", RUN_TITLE), ("b", RUN_TITLE_B)):
        result = runs.create_run(
            RunCreate(
                title=title,
                agent_profile_id=profiles[variant],
                prompt_template_id=prompt_id,
                project_id=project_id,
                inputs={"goal": GOAL},
                source=RunSource.DEMO,
                idempotency_key=f"demo-evidence:{variant}",
                tags=["demo", "evidence"],
                is_demo=True,
            ),
            actor="demo seed",
        )
        report.add("run", title, created=result.created)
        ids.append(result.run.id)
    report.data["evidence_runs"] = ids
    if not (report.execute_inline and _can_execute_inline()):
        return
    settle_demo_runs(ctx)
    if approve_evidence_runs(ctx, ids):
        settle_demo_runs(ctx)
    with ctx.session() as session:
        statuses = session.execute(select(Run.title, Run.status).where(Run.id.in_(ids))).all()
    report.data["evidence_run_status"] = {str(t): str(s) for t, s in statuses}
