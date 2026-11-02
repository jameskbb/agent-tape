"""First-run onboarding state (Journey 1), kept in ``app_settings``.

The flow is offered while no non-demo project exists and the user has neither dismissed
nor completed it. It can always be re-opened from Settings (``reset``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from sqlalchemy import select

from agenttape.application.context import AppContext
from agenttape.application.schemas.library import OnboardingState
from agenttape.application.services.audit import record_audit
from agenttape.application.services.workspaces import get_app_setting, set_app_setting
from agenttape.infrastructure.db.models import AgentProfile, AppSetting, Project

ONBOARDING_KEY = "onboarding"


class OnboardingService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    def state(self) -> OnboardingState:
        with self.ctx.session() as session:
            stored = get_app_setting(session, ONBOARDING_KEY) or {}
            row = session.get(AppSetting, ONBOARDING_KEY)
            has_user_project = (
                session.scalars(
                    select(Project.id).where(
                        Project.is_demo.is_(False), Project.archived.is_(False)
                    )
                ).first()
                is not None
            )
            demo_ready = (
                session.scalars(
                    select(AgentProfile.id).where(
                        AgentProfile.adapter_type == "demo", AgentProfile.archived.is_(False)
                    )
                ).first()
                is not None
            )
            updated: datetime | None = row.updated_at if row is not None else None
        dismissed = bool(stored.get("dismissed"))
        completed = bool(stored.get("completed"))
        return OnboardingState(
            show=not dismissed
            and not completed
            and (not has_user_project or bool(stored.get("reopened"))),
            dismissed=dismissed,
            completed=completed,
            has_user_project=has_user_project,
            demo_ready=demo_ready,
            updated_at=updated,
        )

    def update(
        self, action: Literal["dismiss", "complete", "reset"], *, actor: str = "local user"
    ) -> OnboardingState:
        with self.ctx.session() as session:
            stored = get_app_setting(session, ONBOARDING_KEY) or {}
            if action == "dismiss":
                stored["dismissed"] = True
                stored["reopened"] = False
            elif action == "complete":
                stored["completed"] = True
                stored["reopened"] = False
            else:
                # Re-opened from Settings: show the flow even when user projects exist.
                stored = {"dismissed": False, "completed": False, "reopened": True}
            set_app_setting(session, self.ctx.clock, ONBOARDING_KEY, stored)
            record_audit(
                session,
                self.ctx.clock,
                action=f"onboarding.{action}",
                resource_type="settings",
                resource_id=None,
                actor=actor,
                details={},
            )
        return self.state()
