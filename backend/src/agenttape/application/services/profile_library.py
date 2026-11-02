"""Agent profile administration, secret references (metadata only), and the executable
allowlist."""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import select

from agenttape.adapters.builtin import claude_code, codex_cli
from agenttape.adapters.builtin._process_cli import default_executable_for
from agenttape.application.context import AppContext
from agenttape.application.schemas.catalog import AgentProfileOut
from agenttape.application.schemas.library import (
    AgentProfileUpdate,
    ExecutableAllowlist,
    ExecutableCheckOut,
    ExecutableEntry,
    SecretReferenceCreate,
    SecretReferenceOut,
    SecretReferenceUpdate,
)
from agenttape.application.services.audit import record_audit
from agenttape.application.services.common import get_or_404
from agenttape.application.services.profiles import AgentProfileService
from agenttape.application.services.workspaces import default_workspace_id
from agenttape.domain.errors import ConflictError
from agenttape.infrastructure.db.models import AgentProfile, Project, SecretReference
from agenttape.security import executables


class ProfileLibraryService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------------ profiles

    def update(
        self, profile_id: str, data: AgentProfileUpdate, *, actor: str = "local user"
    ) -> AgentProfileOut:
        """Metadata edits only; configuration changes always create a new version."""
        changes = data.model_dump(exclude_unset=True)
        with self.ctx.session() as session:
            profile = get_or_404(session, AgentProfile, profile_id, "agent profile")
            if "name" in changes and changes["name"] != profile.name:
                clash = session.scalars(
                    select(AgentProfile.id).where(
                        AgentProfile.workspace_id == profile.workspace_id,
                        AgentProfile.name == changes["name"],
                    )
                ).first()
                if clash:
                    raise ConflictError(
                        f"An agent profile named '{changes['name']}' already exists"
                    )
            if changes.get("project_id"):
                get_or_404(session, Project, changes["project_id"], "project")
            for key in ("name", "description", "tags", "project_id", "archived"):
                if key in changes:
                    setattr(profile, key, changes[key])
            profile.updated_at = self.ctx.clock.now()
            action = "agent_profile.update"
            if set(changes) == {"archived"}:
                action = "agent_profile.archive" if changes["archived"] else "agent_profile.restore"
            record_audit(
                session,
                self.ctx.clock,
                action=action,
                resource_type="agent_profile",
                resource_id=profile.id,
                actor=actor,
                workspace_id=profile.workspace_id,
                details={"fields": sorted(changes)},
            )
            session.flush()
        return AgentProfileService(self.ctx).get(profile_id)

    # ------------------------------------------------------------------ secret references

    @staticmethod
    def _secret_out(row: SecretReference) -> SecretReferenceOut:
        out = SecretReferenceOut.model_validate(row)
        out.available = row.provider == "env" and bool(os.environ.get(row.locator))
        return out

    def list_secrets(self) -> list[SecretReferenceOut]:
        with self.ctx.session() as session:
            ws_id = default_workspace_id(session, self.ctx.clock)
            rows = session.scalars(
                select(SecretReference)
                .where(SecretReference.workspace_id == ws_id)
                .order_by(SecretReference.name.asc())
            ).all()
            return [self._secret_out(r) for r in rows]

    def create_secret(
        self, data: SecretReferenceCreate, *, actor: str = "local user"
    ) -> SecretReferenceOut:
        with self.ctx.session() as session:
            ws_id = default_workspace_id(session, self.ctx.clock)
            if session.scalars(
                select(SecretReference.id).where(
                    SecretReference.workspace_id == ws_id, SecretReference.name == data.name
                )
            ).first():
                raise ConflictError(f"A secret reference named '{data.name}' already exists")
            now = self.ctx.clock.now()
            row = SecretReference(
                workspace_id=ws_id,
                name=data.name,
                provider=data.provider,
                locator=data.locator or data.name,
                description=data.description,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            record_audit(
                session,
                self.ctx.clock,
                action="secret_reference.create",
                resource_type="secret_reference",
                resource_id=row.id,
                actor=actor,
                workspace_id=ws_id,
                details={"name": data.name, "provider": data.provider, "locator": row.locator},
            )
            return self._secret_out(row)

    def update_secret(
        self, secret_id: str, data: SecretReferenceUpdate, *, actor: str = "local user"
    ) -> SecretReferenceOut:
        changes = data.model_dump(exclude_unset=True)
        with self.ctx.session() as session:
            row = get_or_404(session, SecretReference, secret_id, "secret reference")
            if changes.get("locator"):
                row.locator = changes["locator"]
            if "description" in changes:
                row.description = changes["description"]
            row.updated_at = self.ctx.clock.now()
            record_audit(
                session,
                self.ctx.clock,
                action="secret_reference.update",
                resource_type="secret_reference",
                resource_id=row.id,
                actor=actor,
                workspace_id=row.workspace_id,
                details={"fields": sorted(changes)},
            )
            session.flush()
            return self._secret_out(row)

    def delete_secret(self, secret_id: str, *, actor: str = "local user") -> None:
        with self.ctx.session() as session:
            row = get_or_404(session, SecretReference, secret_id, "secret reference")
            record_audit(
                session,
                self.ctx.clock,
                action="secret_reference.delete",
                resource_type="secret_reference",
                resource_id=row.id,
                actor=actor,
                workspace_id=row.workspace_id,
                details={"name": row.name},
            )
            session.delete(row)

    # ------------------------------------------------------------------ executables

    def executables(self) -> ExecutableAllowlist:
        data_dir = self.ctx.settings.data_dir
        entries: list[ExecutableEntry] = []
        for source, raw_list in (
            ("env", executables.env_entries()),
            ("settings", executables.stored_entries(data_dir)),
        ):
            for raw in raw_list:
                entries.append(
                    ExecutableEntry(
                        path=raw,
                        source=source,  # type: ignore[arg-type]
                        exists=Path(raw).is_file() and os.access(raw, os.X_OK),
                    )
                )
        return ExecutableAllowlist(
            entries=entries,
            env_var=executables.ENV_VAR,
            stored=executables.stored_entries(data_dir),
        )

    def set_executables(
        self, paths: list[str], *, actor: str = "local user"
    ) -> ExecutableAllowlist:
        cleaned = executables.save_entries(self.ctx.settings.data_dir, paths)
        with self.ctx.session() as session:
            record_audit(
                session,
                self.ctx.clock,
                action="settings.allowed_executables.update",
                resource_type="settings",
                resource_id=None,
                actor=actor,
                details={"executables": cleaned},
            )
        return self.executables()

    def check_executable(self, value: str, adapter_type: str | None = None) -> ExecutableCheckOut:
        defaults: tuple[str, ...] = ()
        if adapter_type == "claude-code":
            defaults = default_executable_for(claude_code.DEFAULT_NAME, claude_code.ENV_OVERRIDE)[1]
        elif adapter_type == "codex-cli":
            defaults = default_executable_for(codex_cli.DEFAULT_NAME, codex_cli.ENV_OVERRIDE)[1]
        result = executables.check_executable(
            value, self.ctx.settings.data_dir, default_names=defaults
        )
        return ExecutableCheckOut(
            requested=result.requested,
            path=str(result.path) if result.path else None,
            allowed=result.ok,
            allowed_by=result.allowed_by,
            error=result.error,
        )
