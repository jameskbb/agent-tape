"""Agent profiles: versioned adapter configuration, validated by the adapter before save."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agenttape.application.context import AppContext
from agenttape.application.schemas.catalog import (
    AdapterInfo,
    AgentProfileCreate,
    AgentProfileOut,
    AgentProfileVersionCreate,
    AgentProfileVersionOut,
    ValidationIssueOut,
)
from agenttape.application.schemas.common import Page
from agenttape.application.services.audit import record_audit
from agenttape.application.services.common import get_or_404, hash_json, like_pattern, paginate
from agenttape.application.services.workspaces import default_workspace_id
from agenttape.domain.errors import ConflictError, ValidationError
from agenttape.evaluations.base import EvaluationSpec
from agenttape.infrastructure.db.models import AgentProfile, AgentProfileVersion

#: Keys understood by the engine itself (adapters add their own).
COMMON_KEYS = {
    "timeout_seconds",
    "idle_timeout_seconds",
    "max_attempts",
    "retry",
    "env_allowlist",
    "secret_refs",
    "evaluators",
    "budget",
    "model",
    "permissions",
    "allowed_tools",
    "denied_tools",
    "workdir_mode",
    "preflight_commands",
    "validation_commands",
    "parser",
}


class AgentProfileService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------------ validation

    def validate_config(
        self, adapter_type: str, config: dict[str, Any]
    ) -> list[ValidationIssueOut]:
        issues: list[ValidationIssueOut] = []
        if not self.ctx.adapters.has(adapter_type):
            return [
                ValidationIssueOut(
                    field="adapter_type", message=f"Unknown adapter '{adapter_type}'"
                )
            ]
        for key in ("timeout_seconds", "idle_timeout_seconds"):
            value = config.get(key)
            if value is not None and (not isinstance(value, int | float) or value <= 0):
                issues.append(ValidationIssueOut(field=key, message="must be a positive number"))
        max_attempts = config.get("max_attempts")
        if max_attempts is not None and (
            not isinstance(max_attempts, int) or not 1 <= max_attempts <= 20
        ):
            issues.append(
                ValidationIssueOut(field="max_attempts", message="must be an integer 1-20")
            )
        for key in ("env_allowlist", "secret_refs"):
            value = config.get(key)
            if value is not None and (
                not isinstance(value, list) or not all(isinstance(v, str) for v in value)
            ):
                issues.append(ValidationIssueOut(field=key, message="must be a list of names"))
        for idx, raw in enumerate(config.get("evaluators") or []):
            try:
                spec = EvaluationSpec.from_dict(raw)
            except (KeyError, TypeError) as exc:
                issues.append(
                    ValidationIssueOut(field=f"evaluators[{idx}]", message=f"invalid: {exc}")
                )
                continue
            if not self.ctx.evaluators.has(spec.type):
                issues.append(
                    ValidationIssueOut(
                        field=f"evaluators[{idx}].type", message=f"unknown evaluator '{spec.type}'"
                    )
                )
                continue
            for problem in self.ctx.evaluators.get(spec.type).validate_config(spec.config):
                issues.append(
                    ValidationIssueOut(field=f"evaluators[{idx}].config", message=problem)
                )
        adapter = self.ctx.adapters.get(adapter_type)
        issues.extend(
            ValidationIssueOut(field=i.field, message=i.message, severity=i.severity)
            for i in adapter.validate_profile(config)
        )
        # Local policy checks (executable allowlist, secret references).
        from agenttape.application.services.profile_policy import policy_issues

        issues.extend(policy_issues(self.ctx, adapter, config))
        return issues

    def _require_valid(self, adapter_type: str, config: dict[str, Any]) -> None:
        errors = [i for i in self.validate_config(adapter_type, config) if i.severity == "error"]
        if errors:
            raise ValidationError(
                "Agent profile configuration is invalid",
                details={"issues": [e.model_dump() for e in errors]},
            )

    # ------------------------------------------------------------------ commands

    def _to_out(self, session: Session, profile: AgentProfile) -> AgentProfileOut:
        out = AgentProfileOut.model_validate(profile)
        latest = session.scalars(
            select(AgentProfileVersion)
            .where(AgentProfileVersion.profile_id == profile.id)
            .order_by(AgentProfileVersion.version.desc())
            .limit(1)
        ).first()
        out.latest = AgentProfileVersionOut.model_validate(latest) if latest else None
        return out

    def create(
        self, data: AgentProfileCreate, *, actor: str = "local user", is_demo: bool = False
    ) -> AgentProfileOut:
        self._require_valid(data.adapter_type, data.config)
        with self.ctx.session() as session:
            ws_id = default_workspace_id(session, self.ctx.clock)
            if session.scalars(
                select(AgentProfile).where(
                    AgentProfile.workspace_id == ws_id, AgentProfile.name == data.name
                )
            ).first():
                raise ConflictError(f"An agent profile named '{data.name}' already exists")
            now = self.ctx.clock.now()
            profile = AgentProfile(
                workspace_id=ws_id,
                project_id=data.project_id,
                name=data.name,
                description=data.description,
                adapter_type=data.adapter_type,
                tags=data.tags,
                latest_version=0,
                is_demo=is_demo,
                created_at=now,
                updated_at=now,
            )
            session.add(profile)
            session.flush()
            self._add_version(
                session,
                profile,
                AgentProfileVersionCreate(config=data.config, notes=data.notes),
                actor=actor,
            )
            record_audit(
                session,
                self.ctx.clock,
                action="agent_profile.create",
                resource_type="agent_profile",
                resource_id=profile.id,
                actor=actor,
                workspace_id=ws_id,
                details={"name": data.name, "adapter_type": data.adapter_type},
            )
            return self._to_out(session, profile)

    def _add_version(
        self,
        session: Session,
        profile: AgentProfile,
        data: AgentProfileVersionCreate,
        *,
        actor: str,
    ) -> AgentProfileVersion:
        profile.latest_version += 1
        version = AgentProfileVersion(
            profile_id=profile.id,
            version=profile.latest_version,
            adapter_type=profile.adapter_type,
            display_name=profile.name,
            description=data.description or profile.description,
            config=data.config,
            config_hash=hash_json({"adapter": profile.adapter_type, "config": data.config}),
            notes=data.notes,
            created_by=actor,
            created_at=self.ctx.clock.now(),
        )
        session.add(version)
        profile.updated_at = self.ctx.clock.now()
        session.flush()
        return version

    def create_version(
        self, profile_id: str, data: AgentProfileVersionCreate, *, actor: str = "local user"
    ) -> AgentProfileVersionOut:
        with self.ctx.session() as session:
            profile = get_or_404(session, AgentProfile, profile_id, "agent profile")
            self._require_valid(profile.adapter_type, data.config)
            version = self._add_version(session, profile, data, actor=actor)
            record_audit(
                session,
                self.ctx.clock,
                action="agent_profile.version.create",
                resource_type="agent_profile",
                resource_id=profile.id,
                actor=actor,
                workspace_id=profile.workspace_id,
                details={"version": version.version},
            )
            return AgentProfileVersionOut.model_validate(version)

    def get(self, profile_id: str) -> AgentProfileOut:
        with self.ctx.session() as session:
            return self._to_out(
                session, get_or_404(session, AgentProfile, profile_id, "agent profile")
            )

    def list_versions(self, profile_id: str) -> list[AgentProfileVersionOut]:
        with self.ctx.session() as session:
            get_or_404(session, AgentProfile, profile_id, "agent profile")
            rows = session.scalars(
                select(AgentProfileVersion)
                .where(AgentProfileVersion.profile_id == profile_id)
                .order_by(AgentProfileVersion.version.desc())
            ).all()
            return [AgentProfileVersionOut.model_validate(r) for r in rows]

    def list_profiles(
        self,
        *,
        search: str | None = None,
        adapter_type: str | None = None,
        include_archived: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> Page[AgentProfileOut]:
        with self.ctx.session() as session:
            stmt = select(AgentProfile)
            if not include_archived:
                stmt = stmt.where(AgentProfile.archived.is_(False))
            if adapter_type:
                stmt = stmt.where(AgentProfile.adapter_type == adapter_type)
            if search:
                stmt = stmt.where(AgentProfile.name.ilike(like_pattern(search), escape="\\"))
            stmt = stmt.order_by(AgentProfile.name.asc())
            return paginate(
                session, stmt, limit=limit, offset=offset, mapper=lambda p: self._to_out(session, p)
            )

    def resolve_version(
        self, session: Session, profile_id: str, version_id: str | None
    ) -> tuple[AgentProfile, AgentProfileVersion]:
        profile = get_or_404(session, AgentProfile, profile_id, "agent profile")
        if version_id:
            version = get_or_404(session, AgentProfileVersion, version_id, "agent profile version")
            if version.profile_id != profile.id:
                raise ValidationError("Profile version does not belong to the profile")
        else:
            latest = session.scalars(
                select(AgentProfileVersion)
                .where(AgentProfileVersion.profile_id == profile.id)
                .order_by(AgentProfileVersion.version.desc())
                .limit(1)
            ).first()
            if latest is None:
                raise ValidationError("Agent profile has no versions")
            version = latest
        return profile, version

    # ------------------------------------------------------------------ adapters

    def adapters(self) -> list[AdapterInfo]:
        infos: list[AdapterInfo] = []
        results = {r.adapter: r for r in self.ctx.adapters.discover_all()}
        for adapter in self.ctx.adapters.all():
            r = results[adapter.name]
            infos.append(
                AdapterInfo(
                    name=adapter.name,
                    display_name=adapter.display_name,
                    description=adapter.description,
                    capabilities=sorted(adapter.capabilities),
                    findings=adapter.findings_capture,
                    status=r.status.value,
                    version=r.version,
                    executable=r.executable,
                    message=r.message,
                    details=r.details,
                    config_schema=adapter.config_schema(),
                )
            )
        return infos
