"""Git isolation demo data: an isolated-worktree demo project and two related runs to compare.

* Project "AgentTape Demo Project (isolated)": the same AgentTape-owned fixture
  repository as the core demo project, with ``git_isolation`` on, a protected path
  (``CHANGELOG.md``) the Demo Agent touches, and two validation commands.
* Run "Improve the greeting service (baseline)": the Demo Agent through the normal run
  engine, in its own ``agenttape/run-<id>`` worktree.
* Its fork with a changed ``goal`` variable, created with :class:`LineageService`.

Both runs really execute (worker hooks capture the diff, flag the protected path, and
run the validations), so the compare view, the diff viewer, and replay all show real
data. The seed drives the queue in-process until both finish (a live worker may claim
some of the work; claims are exclusive). Inside a running event loop it only queues them
and a worker executes them.
"""

from __future__ import annotations

from sqlalchemy import select

from agenttape.adapters.builtin._demo_repo import ensure_fixture_repo
from agenttape.application.context import AppContext
from agenttape.application.schemas.catalog import ProjectCreate, ProjectSettings
from agenttape.application.schemas.git import ForkRequest
from agenttape.application.schemas.runs import RunCreate
from agenttape.application.services.lineage import LineageService
from agenttape.application.services.projects import ProjectService
from agenttape.application.services.runs import RunService
from agenttape.domain.enums import RunSource
from agenttape.infrastructure.db.models import Project, Run
from agenttape.seeds.base import SeedReport
from agenttape.seeds.core_demo import IMPROVE_PROMPT

ORDER = 60

ISOLATED_PROJECT = "AgentTape Demo Project (isolated)"
PARENT_TITLE = "Improve the greeting service (baseline)"
FORK_TITLE = "Improve the greeting service (fork: stricter goal)"
PARENT_GOAL = "Validate the name argument and reject empty names."
FORK_GOAL = (
    "Validate the name argument, reject empty names, and document the behaviour in the "
    "docstring so callers know what to expect."
)
SEED_TIMEOUT_SECONDS = 180.0


def _project_id(ctx: AppContext, report: SeedReport) -> str:
    with ctx.session() as session:
        existing = session.scalars(select(Project).where(Project.name == ISOLATED_PROJECT)).first()
        if existing is not None:
            report.add("project", ISOLATED_PROJECT, created=False)
            return existing.id
    repo = ensure_fixture_repo(ctx.settings.data_dir)
    created = ProjectService(ctx).create(
        ProjectCreate(
            name=ISOLATED_PROJECT,
            repo_path=str(repo),
            description=(
                "The demo repository with Git isolation: every run works in its own "
                "AgentTape-owned worktree, so the diff, validations, and approved commits "
                "never touch the fixture's own checkout."
            ),
            tags=["demo", "git"],
            settings=ProjectSettings(
                timeout_seconds=300,
                git_isolation=True,
                protected_paths=["CHANGELOG.md", ".github/**", "*.lock"],
                validation_commands=[
                    ["python3", "-m", "compileall", "-q", "src", "tests"],
                    ["git", "diff", "--check"],
                ],
            ),
        ),
        actor="demo seed",
        is_demo=True,
    )
    report.add("project", ISOLATED_PROJECT, created=True)
    return created.id


def seed(ctx: AppContext, report: SeedReport) -> None:
    project_id = _project_id(ctx, report)
    if not report.include_runs:
        report.data["isolated_project_id"] = project_id
        return
    report.data["isolated_project_id"] = project_id
    with ctx.session() as session:
        parent = session.scalars(
            select(Run).where(Run.title == PARENT_TITLE, Run.is_demo.is_(True))
        ).first()
        if parent is not None:
            report.add("run", PARENT_TITLE, created=False)
            report.data["compare_runs"] = [parent.id]
            return
    profiles = report.data.get("profiles") or {}
    prompts = report.data.get("prompts") or {}
    if "success" not in profiles or IMPROVE_PROMPT not in prompts:
        return
    runs = RunService(ctx)
    parent_run = runs.create_run(
        RunCreate(
            title=PARENT_TITLE,
            agent_profile_id=profiles["success"],
            prompt_template_id=prompts[IMPROVE_PROMPT],
            project_id=project_id,
            inputs={"goal": PARENT_GOAL},
            source=RunSource.DEMO,
            tags=["demo", "compare"],
            is_demo=True,
        ),
        actor="demo seed",
    ).run
    fork = LineageService(ctx).fork(
        parent_run.id,
        ForkRequest(title=FORK_TITLE, inputs={"goal": FORK_GOAL}),
        actor="demo seed",
    )
    report.add("run", PARENT_TITLE, created=True)
    report.add("run", FORK_TITLE, created=True)
    ids = [parent_run.id, fork.result.run.id]
    report.data["compare_runs"] = ids
    # Executed by the story seed's shared settle step (seeds/story_runs.py), which runs last.
