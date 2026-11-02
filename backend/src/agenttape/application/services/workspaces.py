"""Workspace bootstrap and runtime app settings (allowlisted roots, etc.)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agenttape.config import Settings
from agenttape.infrastructure.clock import Clock
from agenttape.infrastructure.db.models import AppSetting, Workspace
from agenttape.security.paths import normalize_roots

DEFAULT_WORKSPACE_SLUG = "default"
ALLOWED_ROOTS_KEY = "allowed_roots"


def ensure_default_workspace(session: Session, clock: Clock) -> Workspace:
    ws = session.scalars(select(Workspace).where(Workspace.slug == DEFAULT_WORKSPACE_SLUG)).first()
    if ws is not None:
        return ws
    now = clock.now()
    ws = Workspace(
        name="Local workspace",
        slug=DEFAULT_WORKSPACE_SLUG,
        description="Default local workspace",
        is_default=True,
        created_at=now,
        updated_at=now,
    )
    try:
        with session.begin_nested():
            session.add(ws)
    except IntegrityError:
        existing = session.scalars(
            select(Workspace).where(Workspace.slug == DEFAULT_WORKSPACE_SLUG)
        ).one()
        return existing
    return ws


def default_workspace_id(session: Session, clock: Clock) -> str:
    return ensure_default_workspace(session, clock).id


def get_app_setting(session: Session, key: str) -> dict[str, Any] | None:
    row = session.get(AppSetting, key)
    return dict(row.value) if row is not None else None


def set_app_setting(session: Session, clock: Clock, key: str, value: dict[str, Any]) -> None:
    row = session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=value, updated_at=clock.now()))
    else:
        row.value = value
        row.updated_at = clock.now()


def allowed_roots(session: Session, settings: Settings) -> list[Path]:
    """Effective project-root allowlist: env roots + roots saved in Settings + fixture dir.

    The AgentTape fixtures directory is always allowed so the demo project works without
    configuration; it is owned by AgentTape and holds no user data.
    """
    stored = get_app_setting(session, ALLOWED_ROOTS_KEY) or {}
    stored_roots = [Path(p) for p in stored.get("roots", []) if isinstance(p, str)]
    return normalize_roots([*settings.allowed_roots, *stored_roots, settings.fixtures_dir])
