"""Shared API/DTO shapes: pagination and the error envelope."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 50


class OrmModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page[T](BaseModel):
    """Offset pagination used by every list endpoint.

    ``next_offset`` is ``None`` on the last page. ``total`` counts all matching rows.
    """

    items: list[T]
    total: int
    limit: int
    offset: int
    next_offset: int | None = None


class PageParams(BaseModel):
    limit: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)
    offset: int = Field(default=0, ge=0)


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorEnvelope(BaseModel):
    error: ErrorBody
