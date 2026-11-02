"""Global limits stored in the ``limits`` app setting (editable in Settings > Limits)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from agenttape.application.schemas.schedules import LimitsSettings
from agenttape.application.services.workspaces import get_app_setting, set_app_setting
from agenttape.infrastructure.clock import Clock

LIMITS_KEY = "limits"


def load_limits(session: Session) -> LimitsSettings:
    raw = get_app_setting(session, LIMITS_KEY) or {}
    try:
        return LimitsSettings.model_validate(raw)
    except ValueError:
        return LimitsSettings()


def save_limits(session: Session, clock: Clock, limits: LimitsSettings) -> None:
    set_app_setting(session, clock, LIMITS_KEY, limits.model_dump(mode="json"))
