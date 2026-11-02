"""Project path checks and the project activity page.

Schedules and workflows are filled by other services; their sections are queried
tolerantly (a missing table or column degrades to ``available: false``, never a crash).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from agenttape.application.context import AppContext
from agenttape.application.schemas.catalog import AgentProfileOut, PromptTemplateOut
from agenttape.application.schemas.library import (
    ActivitySection,
    PathCheckResult,
    ProjectActivity,
)
from agenttape.application.services.common import get_or_404
from agenttape.application.services.profiles import AgentProfileService
from agenttape.application.services.projects import ProjectService
from agenttape.application.services.prompts import PromptService
from agenttape.application.services.runs import RunService
from agenttape.application.services.workspaces import allowed_roots
from agenttape.domain.errors import AgentTapeError
from agenttape.git.snapshot import capture_git_state
from agenttape.infrastructure.db.models import (
    AgentProfile,
    Project,
    PromptTemplate,
    Run,
    Schedule,
    Workflow,
)
from agenttape.security.paths import canonicalize, is_within

FAILED = ("failed", "timed_out")


class ProjectLibraryService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    def check_path(self, raw: str) -> PathCheckResult:
        """Explain, without side effects, whether ``raw`` can become a project."""
        with self.ctx.session() as session:
            roots = allowed_roots(session, self.ctx.settings)
        root_strs = [str(r) for r in roots]
        base: dict[str, Any] = {
            "input": raw,
            "canonical": None,
            "allowed": False,
            "exists": False,
            "is_dir": False,
            "is_git_repo": False,
            "matched_root": None,
            "allowed_roots": root_strs,
        }
        try:
            path = canonicalize(raw, must_exist=False)
        except AgentTapeError as exc:
            return PathCheckResult(**base, message=exc.message)
        base["canonical"] = str(path)
        base["exists"] = path.exists()
        base["is_dir"] = path.is_dir()
        matched = next((r for r in roots if is_within(path, r)), None)
        base["matched_root"] = str(matched) if matched else None
        if matched is None:
            return PathCheckResult(
                **base,
                message=(
                    "Outside the allowlisted project roots. Add a root in Settings or set "
                    "AGENTTAPE_ALLOWED_ROOTS."
                    if roots
                    else "No project roots are allowlisted yet."
                ),
            )
        if not base["exists"]:
            return PathCheckResult(**base, message="The path does not exist.")
        if not base["is_dir"]:
            return PathCheckResult(**base, message="The path is not a directory.")
        git = capture_git_state(path).as_dict()
        base["is_git_repo"] = bool(git.get("is_repo"))
        base["allowed"] = True
        message = (
            f"Allowed: inside {matched}."
            if path == Path(str(raw)).expanduser()
            else f"Allowed: resolves to {path} (inside {matched})."
        )
        if not base["is_git_repo"]:
            message += " Not a Git repository, so Git features will be unavailable."
        return PathCheckResult(**base, message=message, git=git)

    # ------------------------------------------------------------------ activity

    def _tolerant(self, query: Callable[[], ActivitySection], label: str) -> ActivitySection:
        try:
            return query()
        except SQLAlchemyError as exc:  # a table may be missing
            return ActivitySection(
                available=False, note=f"{label} unavailable: {type(exc).__name__}"
            )

    def activity(self, project_id: str, *, limit: int = 10) -> ProjectActivity:
        project = ProjectService(self.ctx).get(project_id)
        runs_service = RunService(self.ctx)
        runs = runs_service.list_runs(project_id=project_id, limit=limit)
        failures = runs_service.list_runs(project_id=project_id, status=list(FAILED), limit=5)
        with self.ctx.session() as session:
            get_or_404(session, Project, project_id, "project")
            counts = {
                str(status): int(n)
                for status, n in session.execute(
                    select(Run.status, func.count())
                    .where(Run.project_id == project_id)
                    .group_by(Run.status)
                ).all()
            }
            profile_rows = session.scalars(
                select(AgentProfile)
                .where(AgentProfile.project_id == project_id, AgentProfile.archived.is_(False))
                .order_by(AgentProfile.name.asc())
                .limit(50)
            ).all()
            profile_ids = [p.id for p in profile_rows]
            prompt_ids = session.scalars(
                select(PromptTemplate.id)
                .where(PromptTemplate.project_id == project_id, PromptTemplate.archived.is_(False))
                .order_by(PromptTemplate.name.asc())
                .limit(50)
            ).all()

            def schedules() -> ActivitySection:
                with session.begin_nested():
                    rows = session.scalars(
                        select(Schedule)
                        .where(Schedule.project_id == project_id)
                        .order_by(Schedule.name.asc())
                        .limit(limit)
                    ).all()
                    total = session.scalar(
                        select(func.count())
                        .select_from(Schedule)
                        .where(Schedule.project_id == project_id)
                    )
                return ActivitySection(
                    available=True,
                    total=int(total or 0),
                    items=[
                        {
                            "id": s.id,
                            "name": s.name,
                            "enabled": s.enabled,
                            "kind": s.kind,
                            "next_run_at": s.next_run_at.isoformat() if s.next_run_at else None,
                            "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
                        }
                        for s in rows
                    ],
                )

            def workflows() -> ActivitySection:
                with session.begin_nested():
                    rows = session.scalars(
                        select(Workflow)
                        .where(Workflow.project_id == project_id, Workflow.archived.is_(False))
                        .order_by(Workflow.name.asc())
                        .limit(limit)
                    ).all()
                    total = session.scalar(
                        select(func.count())
                        .select_from(Workflow)
                        .where(Workflow.project_id == project_id)
                    )
                return ActivitySection(
                    available=True,
                    total=int(total or 0),
                    items=[
                        {"id": w.id, "name": w.name, "latest_version": w.latest_version}
                        for w in rows
                    ],
                )

            schedule_section = self._tolerant(schedules, "Schedules")
            workflow_section = self._tolerant(workflows, "Workflows")

        profiles_service = AgentProfileService(self.ctx)
        prompts_service = PromptService(self.ctx)
        profiles: list[AgentProfileOut] = [profiles_service.get(pid) for pid in profile_ids]
        prompts: list[PromptTemplateOut] = [prompts_service.get(tid) for tid in prompt_ids]
        return ProjectActivity(
            project=project,
            runs=runs.items,
            runs_total=runs.total,
            status_counts=counts,
            recent_failures=failures.items,
            profiles=profiles,
            prompts=prompts,
            schedules=schedule_section,
            workflows=workflow_section,
        )
