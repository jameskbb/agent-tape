"""Health, readiness, and non-secret system information."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from agenttape import __version__
from agenttape.api.deps import Ctx
from agenttape.application.schemas.catalog import HealthOut, ReadyCheck, ReadyOut
from agenttape.application.services.workspaces import allowed_roots
from agenttape.infrastructure.db.migrate import is_up_to_date

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthOut, summary="Liveness probe")
async def health(ctx: Ctx) -> HealthOut:
    return HealthOut(status="ok", version=__version__, time=ctx.clock.now())


def _ready_checks(ctx: Any) -> list[ReadyCheck]:
    checks: list[ReadyCheck] = []
    try:
        with ctx.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        checks.append(ReadyCheck(name="database", ok=True))
    except Exception as exc:
        checks.append(ReadyCheck(name="database", ok=False, detail=type(exc).__name__))
        return checks
    try:
        ok = is_up_to_date(ctx.engine, ctx.settings.resolved_database_url)
        checks.append(
            ReadyCheck(name="migrations", ok=ok, detail=None if ok else "run `agenttape migrate`")
        )
    except Exception as exc:
        checks.append(ReadyCheck(name="migrations", ok=False, detail=type(exc).__name__))
    data_dir = ctx.settings.data_dir
    checks.append(ReadyCheck(name="data_dir", ok=data_dir.is_dir(), detail=str(data_dir)))
    return checks


@router.get(
    "/ready",
    response_model=ReadyOut,
    summary="Readiness probe (database reachable, migrations applied, data dir present)",
    responses={503: {"model": ReadyOut}},
)
async def ready(ctx: Ctx) -> Any:
    checks = await asyncio.to_thread(_ready_checks, ctx)
    ok = all(c.ok for c in checks)
    body = ReadyOut(status="ready" if ok else "not_ready", checks=checks)
    if not ok:
        return JSONResponse(status_code=503, content=body.model_dump())
    return body


@router.get("/system/info", summary="Runtime information (never includes secrets)")
def system_info(ctx: Ctx) -> dict[str, Any]:
    s = ctx.settings
    with ctx.session() as session:
        roots = [str(r) for r in allowed_roots(session, s)]
    return {
        "version": __version__,
        "data_dir": str(s.data_dir),
        "database": ctx.engine.dialect.name,
        "host": s.host,
        "port": s.port,
        "token_required": s.token_required,
        "allowed_roots": roots,
        "adapters": ctx.adapters.names(),
        "evaluators": [e.type for e in ctx.evaluators.all()],
        "limits": {
            "max_event_payload_bytes": s.max_event_payload_bytes,
            "max_raw_log_bytes": s.max_raw_log_bytes,
            "max_artifact_bytes": s.max_artifact_bytes,
            "default_timeout_seconds": s.default_timeout_seconds,
            "default_idle_timeout_seconds": s.default_idle_timeout_seconds,
            "worker_concurrency": s.worker_concurrency,
        },
    }
