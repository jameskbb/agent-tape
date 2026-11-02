"""Prompt library operations beyond create/version.

* metadata edits, archive and restore;
* the immutability guard: a version that any run has used (``locked_at`` set by
  ``RunService`` at run creation) can never change; an unused version can be amended;
* duplicate, export and import as JSON, text diff between versions;
* search with tag/owner/archived filters;
* "Test with Demo Agent", which creates a real run through ``RunService``.
"""

from __future__ import annotations

import difflib
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from agenttape.application.context import AppContext
from agenttape.application.schemas.catalog import (
    AgentProfileCreate,
    AgentProfileOut,
    PromptTemplateCreate,
    PromptTemplateOut,
    PromptVersionCreate,
    PromptVersionOut,
)
from agenttape.application.schemas.common import Page
from agenttape.application.schemas.library import (
    DiffLine,
    PromptDiff,
    PromptExportBundle,
    PromptImportResult,
    PromptTemplateExport,
    PromptTemplateUpdate,
    PromptTestResult,
    PromptVersionAmend,
    PromptVersionExport,
)
from agenttape.application.schemas.runs import RunCreate
from agenttape.application.services.audit import record_audit
from agenttape.application.services.common import get_or_404, like_pattern
from agenttape.application.services.profiles import AgentProfileService
from agenttape.application.services.prompts import (
    PromptService,
    check_body_against_variables,
    version_hash,
)
from agenttape.application.services.runs import RunService
from agenttape.application.services.workspaces import default_workspace_id
from agenttape.domain.enums import RunSource
from agenttape.domain.errors import ConflictError, NotFoundError, ValidationError
from agenttape.domain.prompts import VariableDefinition
from agenttape.infrastructure.db.models import AgentProfile, Project, PromptTemplate, PromptVersion

ArchivedFilter = Literal["active", "archived", "all"]
DEMO_TEST_PROFILE = "Demo Agent"
MAX_DIFF_LINES = 5000


class ImmutableVersionError(ConflictError):
    code = "version_immutable"


def ensure_version_mutable(version: PromptVersion) -> None:
    """The immutability rule, in one place: used versions never change."""
    if version.locked_at is not None:
        raise ImmutableVersionError(
            f"Version {version.version} has been used by a run and is immutable. "
            "Save your edit as a new version instead.",
            details={"version_id": version.id, "locked_at": version.locked_at.isoformat()},
        )


def _var_map(variables: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(v.get("name")): v for v in variables or []}


class PromptLibraryService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.prompts = PromptService(ctx)

    # ------------------------------------------------------------------ search

    def search(
        self,
        *,
        q: str | None = None,
        tags: list[str] | None = None,
        owner: str | None = None,
        archived: ArchivedFilter = "active",
        project_id: str | None = None,
        sort: str = "name",
        order: str = "asc",
        limit: int = 50,
        offset: int = 0,
    ) -> Page[PromptTemplateOut]:
        with self.ctx.session() as session:
            stmt = select(PromptTemplate)
            if archived == "active":
                stmt = stmt.where(PromptTemplate.archived.is_(False))
            elif archived == "archived":
                stmt = stmt.where(PromptTemplate.archived.is_(True))
            if q:
                pattern = like_pattern(q)
                stmt = stmt.where(
                    PromptTemplate.name.ilike(pattern, escape="\\")
                    | PromptTemplate.description.ilike(pattern, escape="\\")
                )
            if owner:
                stmt = stmt.where(PromptTemplate.owner.ilike(like_pattern(owner), escape="\\"))
            if project_id:
                stmt = stmt.where(PromptTemplate.project_id == project_id)
            column = {
                "name": PromptTemplate.name,
                "updated_at": PromptTemplate.updated_at,
                "created_at": PromptTemplate.created_at,
            }.get(sort, PromptTemplate.name)
            stmt = stmt.order_by(column.desc() if order == "desc" else column.asc())
            rows = list(session.scalars(stmt).all())
            if tags:
                wanted = {t.lower() for t in tags}
                rows = [r for r in rows if wanted <= {str(t).lower() for t in r.tags or []}]
            total = len(rows)
            window = rows[offset : offset + limit]
            items = [self.prompts._to_out(session, t) for t in window]
            next_offset = offset + limit if offset + limit < total else None
            return Page[PromptTemplateOut](
                items=items, total=total, limit=limit, offset=offset, next_offset=next_offset
            )

    def all_tags(self) -> list[str]:
        with self.ctx.session() as session:
            tags: set[str] = set()
            for row in session.scalars(select(PromptTemplate.tags)).all():
                tags.update(str(t) for t in row or [])
            return sorted(tags, key=str.lower)

    # ------------------------------------------------------------------ metadata

    def update(
        self, template_id: str, data: PromptTemplateUpdate, *, actor: str = "local user"
    ) -> PromptTemplateOut:
        changes = data.model_dump(exclude_unset=True)
        with self.ctx.session() as session:
            template = get_or_404(session, PromptTemplate, template_id, "prompt template")
            if "name" in changes and changes["name"] != template.name:
                clash = session.scalars(
                    select(PromptTemplate).where(
                        PromptTemplate.workspace_id == template.workspace_id,
                        PromptTemplate.name == changes["name"],
                    )
                ).first()
                if clash is not None:
                    raise ConflictError(f"A prompt named '{changes['name']}' already exists")
            if changes.get("project_id"):
                get_or_404(session, Project, changes["project_id"], "project")
            for key in ("name", "description", "tags", "owner", "project_id", "archived"):
                if key in changes:
                    setattr(template, key, changes[key])
            template.updated_at = self.ctx.clock.now()
            action = "prompt.update"
            if set(changes) == {"archived"}:
                action = "prompt.archive" if changes["archived"] else "prompt.restore"
            record_audit(
                session,
                self.ctx.clock,
                action=action,
                resource_type="prompt_template",
                resource_id=template.id,
                actor=actor,
                workspace_id=template.workspace_id,
                details={"fields": sorted(changes)},
            )
            session.flush()
            return self.prompts._to_out(session, template)

    # ------------------------------------------------------------------ versions

    def get_version(self, template_id: str, version_id: str) -> PromptVersionOut:
        with self.ctx.session() as session:
            _t, version = self.prompts.resolve_version(session, template_id, version_id)
            return PromptVersionOut.model_validate(version)

    def save_version(
        self, template_id: str, data: PromptVersionCreate, *, actor: str = "local user"
    ) -> PromptVersionOut:
        """Create a new version unless it is identical to the latest one."""
        variables = [v.model_dump(exclude_none=True) for v in data.variables]
        digest = version_hash(data.body, variables, data.settings)
        with self.ctx.session() as session:
            template = get_or_404(session, PromptTemplate, template_id, "prompt template")
            latest = session.scalars(
                select(PromptVersion)
                .where(PromptVersion.template_id == template.id)
                .order_by(PromptVersion.version.desc())
                .limit(1)
            ).first()
            if latest is not None and latest.content_hash == digest:
                raise ConflictError(
                    f"Nothing changed since version {latest.version}",
                    details={"version": latest.version},
                )
        return self.prompts.create_version(template_id, data, actor=actor)

    def amend_version(
        self,
        template_id: str,
        version_id: str,
        data: PromptVersionAmend,
        *,
        actor: str = "local user",
    ) -> PromptVersionOut:
        check_body_against_variables(data.body, data.variables)
        with self.ctx.session() as session:
            template, version = self.prompts.resolve_version(session, template_id, version_id)
            if template.archived:
                raise ConflictError("Archived prompts cannot be edited")
            ensure_version_mutable(version)
            variables = [v.model_dump(exclude_none=True) for v in data.variables]
            version.body = data.body
            version.variables = variables
            version.settings = data.settings
            version.notes = data.notes
            version.content_hash = version_hash(data.body, variables, data.settings)
            template.updated_at = self.ctx.clock.now()
            record_audit(
                session,
                self.ctx.clock,
                action="prompt.version.amend",
                resource_type="prompt_template",
                resource_id=template.id,
                actor=actor,
                workspace_id=template.workspace_id,
                details={"version": version.version},
            )
            session.flush()
            return PromptVersionOut.model_validate(version)

    def diff(self, template_id: str, from_version: int, to_version: int) -> PromptDiff:
        with self.ctx.session() as session:
            get_or_404(session, PromptTemplate, template_id, "prompt template")
            rows = {
                v.version: v
                for v in session.scalars(
                    select(PromptVersion).where(
                        PromptVersion.template_id == template_id,
                        PromptVersion.version.in_([from_version, to_version]),
                    )
                ).all()
            }
            for number in (from_version, to_version):
                if number not in rows:
                    raise NotFoundError("prompt version", str(number))
            old, new = rows[from_version], rows[to_version]
            a = old.body.splitlines()
            b = new.body.splitlines()
            unified = "\n".join(
                difflib.unified_diff(a, b, f"v{from_version}", f"v{to_version}", lineterm="")
            )
            lines: list[DiffLine] = []
            old_no = new_no = 0
            for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
                a=a, b=b, autojunk=False
            ).get_opcodes():
                if tag == "equal":
                    for k in range(i2 - i1):
                        old_no, new_no = i1 + k + 1, j1 + k + 1
                        lines.append(
                            DiffLine(op="context", text=a[i1 + k], old_no=old_no, new_no=new_no)
                        )
                    continue
                for k in range(i1, i2):
                    lines.append(DiffLine(op="remove", text=a[k], old_no=k + 1))
                for k in range(j1, j2):
                    lines.append(DiffLine(op="add", text=b[k], new_no=k + 1))
                if len(lines) > MAX_DIFF_LINES:
                    break
            va, vb = _var_map(old.variables), _var_map(new.variables)
            return PromptDiff(
                template_id=template_id,
                from_version=from_version,
                to_version=to_version,
                unified=unified,
                lines=lines[:MAX_DIFF_LINES],
                variables_added=sorted(set(vb) - set(va)),
                variables_removed=sorted(set(va) - set(vb)),
                variables_changed=sorted(n for n in set(va) & set(vb) if va[n] != vb[n]),
                body_changed=old.body != new.body,
            )

    # ------------------------------------------------------------------ duplicate / export

    def _unique_name(self, session: Session, ws_id: str, base: str) -> str:
        name, n = base, 2
        while session.scalars(
            select(PromptTemplate.id).where(
                PromptTemplate.workspace_id == ws_id, PromptTemplate.name == name
            )
        ).first():
            name = f"{base} ({n})"
            n += 1
        return name[:200]

    def duplicate(
        self,
        template_id: str,
        *,
        name: str | None = None,
        version_id: str | None = None,
        actor: str = "local user",
    ) -> PromptTemplateOut:
        with self.ctx.session() as session:
            template, version = self.prompts.resolve_version(session, template_id, version_id)
            new_name = name or self._unique_name(
                session, template.workspace_id, f"{template.name} (copy)"
            )
            data = PromptTemplateCreate(
                name=new_name,
                description=template.description,
                tags=list(template.tags or []),
                owner=template.owner,
                project_id=template.project_id,
                body=version.body,
                variables=[VariableDefinition.model_validate(v) for v in version.variables or []],
                settings=dict(version.settings or {}),
                notes=f"Duplicated from '{template.name}' v{version.version}",
            )
        created = self.prompts.create_template(
            data,
            actor=actor,
        )
        with self.ctx.session() as session:
            row = get_or_404(session, PromptTemplate, created.id, "prompt template")
            row.duplicated_from_id = template_id
            session.flush()
            return self.prompts._to_out(session, row)

    def export(self, template_ids: list[str] | None = None) -> PromptExportBundle:
        with self.ctx.session() as session:
            stmt = select(PromptTemplate).order_by(PromptTemplate.name.asc())
            if template_ids:
                stmt = stmt.where(PromptTemplate.id.in_(template_ids))
            templates = session.scalars(stmt).all()
            if template_ids and len(templates) != len(set(template_ids)):
                raise NotFoundError("prompt template", ",".join(template_ids))
            out: list[PromptTemplateExport] = []
            for t in templates:
                versions = session.scalars(
                    select(PromptVersion)
                    .where(PromptVersion.template_id == t.id)
                    .order_by(PromptVersion.version.asc())
                ).all()
                out.append(
                    PromptTemplateExport(
                        name=t.name,
                        description=t.description,
                        tags=list(t.tags or []),
                        owner=t.owner,
                        archived=t.archived,
                        versions=[
                            PromptVersionExport(
                                version=v.version,
                                body=v.body,
                                variables=list(v.variables or []),
                                settings=dict(v.settings or {}),
                                notes=v.notes,
                                content_hash=v.content_hash,
                                created_at=v.created_at,
                            )
                            for v in versions
                        ],
                    )
                )
        return PromptExportBundle(exported_at=self.ctx.clock.now(), templates=out)

    def import_bundle(
        self,
        bundle: PromptExportBundle,
        *,
        on_conflict: Literal["skip", "rename"] = "rename",
        actor: str = "local user",
    ) -> PromptImportResult:
        """Import templates with their full version history. Nothing is overwritten."""
        # Validate everything first so a bad bundle imports nothing.
        parsed: list[tuple[PromptTemplateExport, list[list[VariableDefinition]]]] = []
        for idx, t in enumerate(bundle.templates):
            per_version: list[list[VariableDefinition]] = []
            for v in sorted(t.versions, key=lambda x: x.version):
                try:
                    defs = [VariableDefinition.model_validate(d) for d in v.variables]
                    check_body_against_variables(v.body, defs)
                except ValidationError as exc:
                    raise ValidationError(
                        f"Template {idx} ('{t.name}') version {v.version}: {exc.message}",
                        details=exc.details,
                    ) from exc
                except ValueError as exc:
                    raise ValidationError(
                        f"Template {idx} ('{t.name}') version {v.version}: {exc}"
                    ) from exc
                per_version.append(defs)
            parsed.append((t, per_version))

        created: list[PromptTemplateOut] = []
        skipped: list[dict[str, str]] = []
        for t, per_version in parsed:
            with self.ctx.session() as session:
                ws_id = default_workspace_id(session, self.ctx.clock)
                exists = session.scalars(
                    select(PromptTemplate.id).where(
                        PromptTemplate.workspace_id == ws_id, PromptTemplate.name == t.name
                    )
                ).first()
                name = t.name
                if exists:
                    if on_conflict == "skip":
                        skipped.append({"name": t.name, "reason": "a prompt with this name exists"})
                        continue
                    name = self._unique_name(session, ws_id, f"{t.name} (imported)")
            versions = sorted(t.versions, key=lambda x: x.version)
            first = versions[0]
            out = self.prompts.create_template(
                PromptTemplateCreate(
                    name=name,
                    description=t.description,
                    tags=t.tags,
                    owner=t.owner,
                    body=first.body,
                    variables=per_version[0],
                    settings=first.settings,
                    notes=first.notes or f"Imported (original v{first.version})",
                ),
                actor=actor,
            )
            for v, defs in zip(versions[1:], per_version[1:], strict=True):
                self.prompts.create_version(
                    out.id,
                    PromptVersionCreate(
                        body=v.body,
                        variables=defs,
                        settings=v.settings,
                        notes=v.notes or f"Imported (original v{v.version})",
                    ),
                    actor=actor,
                )
            if t.archived:
                self.update(out.id, PromptTemplateUpdate(archived=True), actor=actor)
            created.append(self.prompts.get(out.id))
        return PromptImportResult(created=created, skipped=skipped)

    # ------------------------------------------------------------------ test with demo agent

    def demo_profile(self) -> AgentProfileOut:
        """The Demo Agent profile used for prompt tests (created if demo data was reset)."""
        with self.ctx.session() as session:
            rows = session.scalars(
                select(AgentProfile)
                .where(AgentProfile.adapter_type == "demo", AgentProfile.archived.is_(False))
                .order_by(AgentProfile.created_at.asc())
            ).all()
            chosen = next((p for p in rows if p.name == DEMO_TEST_PROFILE), None)
            if chosen is None:
                service = AgentProfileService(self.ctx)
                for p in rows:
                    _p, version = service.resolve_version(session, p.id, None)
                    if (version.config or {}).get("scenario", "success") == "success":
                        chosen = p
                        break
            chosen_id = chosen.id if chosen else None
        profiles = AgentProfileService(self.ctx)
        if chosen_id:
            return profiles.get(chosen_id)
        return profiles.create(
            AgentProfileCreate(
                name="Demo Agent (prompt tests)",
                adapter_type="demo",
                description="Created automatically to test prompts without credentials.",
                config={"scenario": "success"},
            ),
            actor="agenttape",
        )

    def test_with_demo(
        self,
        template_id: str,
        inputs: dict[str, Any],
        *,
        version_id: str | None = None,
        actor: str = "local user",
    ) -> PromptTestResult:
        profile = self.demo_profile()
        with self.ctx.session() as session:
            template, version = self.prompts.resolve_version(session, template_id, version_id)
            if template.archived:
                raise ConflictError("Restore this prompt before testing it")
            project_id = None
            if template.project_id:
                project = session.get(Project, template.project_id)
                if project is not None and not project.archived:
                    project_id = project.id
            resolved_version_id = version.id
            title = f"Prompt test: {template.name} v{version.version}"
        result = RunService(self.ctx).create_run(
            RunCreate(
                agent_profile_id=profile.id,
                prompt_template_id=template_id,
                prompt_version_id=resolved_version_id,
                inputs=inputs,
                project_id=project_id,
                source=RunSource.UI,
                source_ref=f"prompt-test:{template_id}",
                title=title,
                tags=["prompt-test"],
            ),
            actor=actor,
        )
        return PromptTestResult(run=result.run, profile=profile)
