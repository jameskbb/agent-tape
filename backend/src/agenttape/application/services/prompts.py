"""Prompt templates and immutable versions with typed variables."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agenttape.application.context import AppContext
from agenttape.application.schemas.catalog import (
    PromptTemplateCreate,
    PromptTemplateOut,
    PromptVersionCreate,
    PromptVersionOut,
    RenderPreview,
)
from agenttape.application.services.audit import record_audit
from agenttape.application.services.common import get_or_404, hash_json
from agenttape.application.services.workspaces import default_workspace_id
from agenttape.domain.errors import ConflictError, ValidationError
from agenttape.domain.prompts import (
    RenderMode,
    VariableDefinition,
    render_template,
    template_tokens,
    validate_inputs,
)
from agenttape.infrastructure.db.models import PromptTemplate, PromptVersion
from agenttape.security.redaction import DEFAULT_REDACTOR, RedactionReport

_TEMPLATE_TOKEN = re.compile(r"\{\{[^{}]*\}\}")


def _reject_credentials(body: str, variables: list[VariableDefinition]) -> None:
    """Prompt versions are stored and exported; credential-shaped text must never be."""
    report = RedactionReport()
    # Template tokens ({{ name }}, {{ secret:NAME }}) are placeholders, not values.
    DEFAULT_REDACTOR.redact_text(_TEMPLATE_TOKEN.sub("{{}}", body), report)
    for v in variables:
        if isinstance(v.default, str):
            DEFAULT_REDACTOR.redact_text(v.default, report)
    if report.count:
        raise ValidationError(
            "The prompt contains something that looks like a credential. Reference secrets "
            "by name with {{ secret:NAME }} instead; values are resolved only when a run starts.",
            details={"kinds": sorted(report.kinds)},
        )


def check_body_against_variables(body: str, variables: list[VariableDefinition]) -> None:
    """Every ``{{ name }}`` in the body must be declared; names must be unique."""
    names = [v.name for v in variables]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ValidationError("Duplicate variable names", details={"variables": dupes})
    _reject_credentials(body, variables)
    used, _secrets = template_tokens(body)
    undeclared = [n for n in used if n not in names]
    if undeclared:
        raise ValidationError(
            "The template uses variables that are not declared",
            details={"undeclared": undeclared},
        )


def version_hash(body: str, variables: list[dict[str, Any]], settings: dict[str, Any]) -> str:
    return hash_json({"body": body, "variables": variables, "settings": settings})


class PromptService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    def _to_out(self, session: Session, template: PromptTemplate) -> PromptTemplateOut:
        out = PromptTemplateOut.model_validate(template)
        latest = session.scalars(
            select(PromptVersion)
            .where(PromptVersion.template_id == template.id)
            .order_by(PromptVersion.version.desc())
            .limit(1)
        ).first()
        out.latest = PromptVersionOut.model_validate(latest) if latest else None
        return out

    def create_template(
        self, data: PromptTemplateCreate, *, actor: str = "local user", is_demo: bool = False
    ) -> PromptTemplateOut:
        check_body_against_variables(data.body, data.variables)
        with self.ctx.session() as session:
            ws_id = default_workspace_id(session, self.ctx.clock)
            if session.scalars(
                select(PromptTemplate).where(
                    PromptTemplate.workspace_id == ws_id, PromptTemplate.name == data.name
                )
            ).first():
                raise ConflictError(f"A prompt named '{data.name}' already exists")
            now = self.ctx.clock.now()
            template = PromptTemplate(
                workspace_id=ws_id,
                project_id=data.project_id,
                name=data.name,
                description=data.description,
                tags=data.tags,
                owner=data.owner,
                latest_version=0,
                is_demo=is_demo,
                created_at=now,
                updated_at=now,
            )
            session.add(template)
            session.flush()
            self._add_version(
                session,
                template,
                PromptVersionCreate(
                    body=data.body,
                    variables=data.variables,
                    settings=data.settings,
                    notes=data.notes,
                ),
                actor=actor,
            )
            record_audit(
                session,
                self.ctx.clock,
                action="prompt.create",
                resource_type="prompt_template",
                resource_id=template.id,
                actor=actor,
                workspace_id=ws_id,
                details={"name": data.name},
            )
            return self._to_out(session, template)

    def _add_version(
        self, session: Session, template: PromptTemplate, data: PromptVersionCreate, *, actor: str
    ) -> PromptVersion:
        variables = [v.model_dump(exclude_none=True) for v in data.variables]
        template.latest_version += 1
        version = PromptVersion(
            template_id=template.id,
            version=template.latest_version,
            body=data.body,
            variables=variables,
            settings=data.settings,
            content_hash=version_hash(data.body, variables, data.settings),
            notes=data.notes,
            created_by=actor,
            created_at=self.ctx.clock.now(),
        )
        session.add(version)
        template.updated_at = self.ctx.clock.now()
        session.flush()
        return version

    def create_version(
        self, template_id: str, data: PromptVersionCreate, *, actor: str = "local user"
    ) -> PromptVersionOut:
        """Every edit is a new version; existing versions are never modified."""
        check_body_against_variables(data.body, data.variables)
        with self.ctx.session() as session:
            template = get_or_404(session, PromptTemplate, template_id, "prompt template")
            if template.archived:
                raise ConflictError("Archived prompts cannot receive new versions")
            version = self._add_version(session, template, data, actor=actor)
            record_audit(
                session,
                self.ctx.clock,
                action="prompt.version.create",
                resource_type="prompt_template",
                resource_id=template.id,
                actor=actor,
                workspace_id=template.workspace_id,
                details={"version": version.version},
            )
            return PromptVersionOut.model_validate(version)

    def get(self, template_id: str) -> PromptTemplateOut:
        with self.ctx.session() as session:
            return self._to_out(
                session, get_or_404(session, PromptTemplate, template_id, "prompt template")
            )

    def list_versions(self, template_id: str) -> list[PromptVersionOut]:
        with self.ctx.session() as session:
            get_or_404(session, PromptTemplate, template_id, "prompt template")
            rows = session.scalars(
                select(PromptVersion)
                .where(PromptVersion.template_id == template_id)
                .order_by(PromptVersion.version.desc())
            ).all()
            return [PromptVersionOut.model_validate(r) for r in rows]

    def resolve_version(
        self, session: Session, template_id: str, version_id: str | None
    ) -> tuple[PromptTemplate, PromptVersion]:
        template = get_or_404(session, PromptTemplate, template_id, "prompt template")
        if version_id:
            version = get_or_404(session, PromptVersion, version_id, "prompt version")
            if version.template_id != template.id:
                raise ValidationError("Prompt version does not belong to the template")
        else:
            latest = session.scalars(
                select(PromptVersion)
                .where(PromptVersion.template_id == template.id)
                .order_by(PromptVersion.version.desc())
                .limit(1)
            ).first()
            if latest is None:
                raise ValidationError("Prompt template has no versions")
            version = latest
        return template, version

    def preview(
        self, template_id: str, inputs: dict[str, Any], version_id: str | None = None
    ) -> RenderPreview:
        """Render with secret markers (never values) and report variable issues."""
        with self.ctx.session() as session:
            _template, version = self.resolve_version(session, template_id, version_id)
            definitions = [VariableDefinition.model_validate(v) for v in version.variables]
            values, issues = validate_inputs(definitions, inputs)
            rendered = render_template(version.body, definitions, values, mode=RenderMode.REDACT)
            return RenderPreview(
                text=rendered.text,
                secret_refs=rendered.secret_refs,
                issues=[i.model_dump() for i in issues],
                valid=not issues,
            )
