"""FastAPI dependencies shared by routers."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query, Request

from agenttape.application.context import AppContext
from agenttape.application.schemas.common import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, PageParams


def get_ctx(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    return ctx


Ctx = Annotated[AppContext, Depends(get_ctx)]


def page_params(
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PageParams:
    return PageParams(limit=limit, offset=offset)


Paging = Annotated[PageParams, Depends(page_params)]


def get_actor(request: Request) -> str:
    """Display name for audit entries. Local single-user V1: optional ``X-AgentTape-Actor``."""
    actor = request.headers.get("x-agenttape-actor", "").strip()
    return actor[:200] if actor else "local user"


Actor = Annotated[str, Depends(get_actor)]
