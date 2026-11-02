"""Helpers shared by application services."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from agenttape.application.schemas.common import Page
from agenttape.domain.enums import TERMINAL_STATUS_VALUES
from agenttape.domain.errors import NotFoundError, RunNotFinishedError
from agenttape.infrastructure.db.base import Base

if TYPE_CHECKING:
    from agenttape.infrastructure.db.models import Run

M = TypeVar("M", bound=Base)
T = TypeVar("T")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def hash_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def get_or_404(session: Session, model: type[M], id_: str, resource: str) -> M:
    obj = session.get(model, id_)
    if obj is None:
        raise NotFoundError(resource, id_)
    return obj


def require_finished(run: Run, message: str) -> None:
    """Raise the 409 ``run_not_finished`` with ``message`` unless ``run`` has ended."""
    if run.status not in TERMINAL_STATUS_VALUES:
        raise RunNotFinishedError(message)


def paginate(
    session: Session,
    stmt: Select[Any],
    *,
    limit: int,
    offset: int,
    mapper: Callable[[Any], T],
) -> Page[T]:
    """Run ``stmt`` (already filtered and ordered) with a total count and offset window."""
    total = int(
        session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    )
    rows = session.scalars(stmt.limit(limit).offset(offset)).all()
    items = [mapper(r) for r in rows]
    next_offset = offset + limit if offset + limit < total else None
    return Page[T](items=items, total=total, limit=limit, offset=offset, next_offset=next_offset)


def like_pattern(text: str) -> str:
    """Escape ``%``/``_`` for a portable case-insensitive LIKE search."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped.lower()}%"
