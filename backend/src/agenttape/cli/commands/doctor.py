"""``agenttape doctor``: diagnose the local installation without changing anything."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy import Engine, func, select, text

from agenttape.adapters.registry import AdapterRegistry
from agenttape.api.app import WEB_DIST
from agenttape.cli.common import echo_json
from agenttape.config import load_settings
from agenttape.domain.enums import AdapterAvailability
from agenttape.infrastructure.db.engine import make_engine
from agenttape.infrastructure.db.migrate import current_revisions, script_heads

OK = typer.style("ok  ", fg=typer.colors.GREEN, bold=True)
WARN = typer.style("warn", fg=typer.colors.YELLOW, bold=True)
FAIL = typer.style("FAIL", fg=typer.colors.RED, bold=True)


def _runtime_checks(engine: Engine) -> list[tuple[str, str, str]]:
    """Scheduler lease holder, live workers, and queue depth (read-only)."""
    from agenttape.application.services.schedules import SCHEDULER_LEASE
    from agenttape.infrastructure.db.models import QueueItem, SchedulerLease, WorkerHeartbeat

    def aware(value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    now = datetime.now(UTC)
    out: list[tuple[str, str, str]] = []
    with engine.connect() as conn:
        lease = conn.execute(
            select(SchedulerLease.owner_id, SchedulerLease.expires_at).where(
                SchedulerLease.name == SCHEDULER_LEASE
            )
        ).first()
        if lease is None:
            out.append(
                ("scheduler", "warn", "no scheduler has run yet; `agenttape serve` starts one")
            )
        elif aware(lease[1]) > now:
            out.append(
                (
                    "scheduler",
                    "ok",
                    f"lease held by {lease[0]} until {aware(lease[1]):%H:%M:%S} UTC",
                )
            )
        else:
            out.append(
                (
                    "scheduler",
                    "warn",
                    f"lease expired {aware(lease[1]):%Y-%m-%d %H:%M} UTC; no scheduler is running",
                )
            )
        rows = conn.execute(
            select(
                WorkerHeartbeat.worker_id,
                WorkerHeartbeat.kind,
                WorkerHeartbeat.active_count,
                WorkerHeartbeat.capacity,
                WorkerHeartbeat.last_heartbeat_at,
            ).where(WorkerHeartbeat.status == "running")
        ).all()
        live = [
            r for r in rows if aware(r[4]) >= now - timedelta(seconds=60) and r[1] != "scheduler"
        ]
        if live:
            detail = ", ".join(f"{r[0]} ({r[2]}/{r[3]} busy)" for r in live)
            out.append(("workers", "ok", f"{len(live)} live: {detail}"))
        else:
            out.append(
                (
                    "workers",
                    "warn",
                    "no live worker; queued runs wait for `agenttape serve` or `agenttape worker`",
                )
            )
        queued = conn.execute(
            select(func.count()).select_from(QueueItem).where(QueueItem.status == "queued")
        ).scalar_one()
        out.append(("queue", "ok", f"{queued} item(s) waiting"))
    return out


def register(app: typer.Typer) -> None:
    @app.command()
    def doctor(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
        """Check Python, data dir, database, migrations, git, adapters, UI build, and bind safety."""
        settings = load_settings()
        results: list[dict[str, str]] = []

        def add(name: str, level: str, detail: str) -> None:
            results.append({"check": name, "level": level, "detail": detail})

        py = sys.version_info
        add("python", "ok" if py >= (3, 12) else "fail", f"{py.major}.{py.minor}.{py.micro}")

        data_dir = settings.data_dir
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=data_dir, prefix=".doctor-", delete=True):
                pass
            add("data_dir", "ok", f"{data_dir} is writable")
        except OSError as exc:
            add("data_dir", "fail", f"{data_dir}: {exc}")

        url = settings.resolved_database_url
        try:
            engine = make_engine(url)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            add("database", "ok", engine.url.render_as_string(hide_password=True))
            current = current_revisions(engine)
            heads = script_heads(url)
            if current == heads:
                add("migrations", "ok", f"at {', '.join(sorted(heads))}")
            elif not current:
                add("migrations", "warn", "database not initialised; run `agenttape init`")
            else:
                add(
                    "migrations",
                    "warn",
                    f"at {sorted(current)}, latest {sorted(heads)}; run `agenttape migrate`",
                )
            if current:
                for name, level, detail in _runtime_checks(engine):
                    add(name, level, detail)
            engine.dispose()
        except Exception as exc:
            add("database", "fail", f"{type(exc).__name__}: {exc}")

        git = shutil.which("git")
        add(
            "git",
            "ok" if git else "warn",
            git or "git not found; Git features and the demo diff are unavailable",
        )

        for result in AdapterRegistry.load().discover_all():
            level = (
                "ok"
                if result.status in (AdapterAvailability.READY, AdapterAvailability.INSTALLED)
                else "warn"
            )
            detail = f"{result.status.value}" + (f" {result.version}" if result.version else "")
            if result.message:
                detail += f" - {result.message}"
            add(f"adapter:{result.adapter}", level, detail)

        index = Path(WEB_DIST) / "index.html"
        add(
            "web_ui",
            "ok" if index.is_file() else "warn",
            str(index) if index.is_file() else "not built; run `make build`",
        )

        if settings.binds_beyond_loopback:
            has_token = settings.effective_token_hash() is not None
            add(
                "bind",
                "ok" if has_token else "fail",
                f"{settings.host} is beyond loopback; "
                + ("token configured" if has_token else "NO token configured"),
            )
        else:
            add("bind", "ok", f"{settings.host} (loopback only)")
        if settings.allowed_roots:
            add("allowed_roots", "ok", os.pathsep.join(str(r) for r in settings.allowed_roots))
        else:
            add(
                "allowed_roots",
                "warn",
                "AGENTTAPE_ALLOWED_ROOTS is empty; add roots in Settings or the env",
            )

        if as_json:
            echo_json(results)
        else:
            for r in results:
                badge = {"ok": OK, "warn": WARN, "fail": FAIL}[r["level"]]
                typer.echo(f"{badge}  {r['check']:<22} {r['detail']}")
        if any(r["level"] == "fail" for r in results):
            raise typer.Exit(1)
