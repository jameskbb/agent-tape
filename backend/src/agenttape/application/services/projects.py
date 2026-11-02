"""Projects: allowlisted canonical repository paths plus execution settings."""

from __future__ import annotations

from sqlalchemy import select

from agenttape.application.context import AppContext
from agenttape.application.schemas.catalog import ProjectCreate, ProjectOut, ProjectUpdate
from agenttape.application.schemas.common import Page
from agenttape.application.services.audit import record_audit
from agenttape.application.services.common import get_or_404, like_pattern, paginate
from agenttape.application.services.workspaces import allowed_roots, default_workspace_id
from agenttape.domain.errors import ConflictError
from agenttape.git.snapshot import capture_git_state
from agenttape.infrastructure.db.models import Project
from agenttape.security.paths import ensure_allowed


class ProjectService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    def create(
        self, data: ProjectCreate, *, actor: str = "local user", is_demo: bool = False
    ) -> ProjectOut:
        with self.ctx.session() as session:
            path = ensure_allowed(data.repo_path, allowed_roots(session, self.ctx.settings))
            if not path.is_dir():
                raise ConflictError("Project path must be a directory", details={"path": str(path)})
            ws_id = default_workspace_id(session, self.ctx.clock)
            if session.scalars(
                select(Project).where(Project.workspace_id == ws_id, Project.name == data.name)
            ).first():
                raise ConflictError(f"A project named '{data.name}' already exists")
            git = capture_git_state(path)
            now = self.ctx.clock.now()
            project = Project(
                workspace_id=ws_id,
                name=data.name,
                description=data.description,
                repo_path=str(path),
                default_branch=data.default_branch or git.branch,
                tags=data.tags,
                settings=data.settings.model_dump(),
                git_state=git.as_dict(),
                git_checked_at=now,
                is_demo=is_demo,
                created_at=now,
                updated_at=now,
            )
            session.add(project)
            session.flush()
            record_audit(
                session,
                self.ctx.clock,
                action="project.create",
                resource_type="project",
                resource_id=project.id,
                actor=actor,
                workspace_id=ws_id,
                details={"name": data.name, "path": str(path)},
            )
            return ProjectOut.model_validate(project)

    def update(
        self, project_id: str, data: ProjectUpdate, *, actor: str = "local user"
    ) -> ProjectOut:
        with self.ctx.session() as session:
            project = get_or_404(session, Project, project_id, "project")
            changes = data.model_dump(exclude_unset=True)
            if "settings" in changes and data.settings is not None:
                project.settings = data.settings.model_dump()
            for key in ("name", "description", "default_branch", "tags", "archived"):
                if key in changes:
                    setattr(project, key, changes[key])
            record_audit(
                session,
                self.ctx.clock,
                action="project.update",
                resource_type="project",
                resource_id=project.id,
                actor=actor,
                workspace_id=project.workspace_id,
                details={"fields": sorted(changes)},
            )
            session.flush()
            return ProjectOut.model_validate(project)

    def get(self, project_id: str) -> ProjectOut:
        with self.ctx.session() as session:
            return ProjectOut.model_validate(get_or_404(session, Project, project_id, "project"))

    def list_projects(
        self,
        *,
        search: str | None = None,
        include_archived: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> Page[ProjectOut]:
        with self.ctx.session() as session:
            stmt = select(Project)
            if not include_archived:
                stmt = stmt.where(Project.archived.is_(False))
            if search:
                stmt = stmt.where(Project.name.ilike(like_pattern(search), escape="\\"))
            stmt = stmt.order_by(Project.name.asc())
            return paginate(
                session, stmt, limit=limit, offset=offset, mapper=ProjectOut.model_validate
            )

    def refresh_git_state(self, project_id: str) -> ProjectOut:
        with self.ctx.session() as session:
            project = get_or_404(session, Project, project_id, "project")
            project.git_state = capture_git_state(project.repo_path).as_dict()
            project.git_checked_at = self.ctx.clock.now()
            session.flush()
            return ProjectOut.model_validate(project)
