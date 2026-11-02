"""``agenttape scheduler`` (standalone scheduler process) and ``agenttape schedules ...``.

Every command calls :class:`ScheduleService`, the same service the HTTP API uses.
"""

from __future__ import annotations

import asyncio
import signal
from typing import Annotated, Literal

import typer
from sqlalchemy import or_, select

from agenttape.application.context import AppContext
from agenttape.application.schemas.schedules import SchedulePreviewRequest
from agenttape.application.services.schedules import ScheduleService
from agenttape.cli.common import call, echo_json, get_context, print_table, status_text
from agenttape.domain.errors import NotFoundError
from agenttape.infrastructure.db.models import Schedule
from agenttape.scheduler.engine import SchedulerLoop


def _resolve(ctx: AppContext, ref: str) -> str:
    with ctx.session() as session:
        row = session.scalars(
            select(Schedule).where(or_(Schedule.id == ref, Schedule.name == ref))
        ).first()
        if row is None:
            raise NotFoundError("schedule", ref)
        return row.id


def _when(value: str | None) -> str:
    return value[:16].replace("T", " ") if value else "-"


def register(app: typer.Typer) -> None:
    @app.command()
    def scheduler(
        interval: Annotated[
            float, typer.Option(help="Seconds between scheduling passes", min=0.2)
        ] = 2.0,
    ) -> None:
        """Run a standalone scheduler process (one active owner via a database lease)."""
        ctx = get_context()
        loop = SchedulerLoop(ctx, interval=interval)
        typer.echo(
            f"Scheduler {loop.engine.owner_id} running (tick every {interval:g}s). "
            "If another scheduler holds the lease this one waits as a standby. Ctrl+C to stop."
        )

        async def main() -> None:
            stop = asyncio.Event()
            ev_loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                ev_loop.add_signal_handler(sig, stop.set)
            await loop.run(stop)

        asyncio.run(main())

    schedules = typer.Typer(help="List and control schedules.", no_args_is_help=True)
    app.add_typer(schedules, name="schedules")

    @schedules.command("list")
    def list_(
        as_json: Annotated[bool, typer.Option("--json", help="Print JSON")] = False,
        search: Annotated[str | None, typer.Option("--search", "-q")] = None,
    ) -> None:
        """List schedules with their next and last run."""
        ctx = get_context()
        page = call(lambda: ScheduleService(ctx).list_schedules(search=search, limit=200))
        if as_json:
            echo_json([s.model_dump(mode="json") for s in page.items])
            return
        if not page.items:
            typer.echo("No schedules yet. Create one in the web UI (Schedules) or via the API.")
            return
        rows = []
        for s in page.items:
            timing = (
                s.cron_expression
                if s.kind == "cron"
                else f"every {s.interval_seconds}s"
                if s.kind == "interval"
                else f"once {_when(s.run_at_local)}"
            )
            rows.append(
                [
                    s.id[:8],
                    s.name,
                    "enabled" if s.enabled else "paused",
                    f"{s.kind}: {timing}",
                    s.timezone,
                    _when(s.next_run_local) if s.enabled else "-",
                    status_text(s.last_run_status) if s.last_run_status else "-",
                ]
            )
        print_table(rows, ["ID", "NAME", "STATE", "WHEN", "ZONE", "NEXT (LOCAL)", "LAST RUN"])
        status = ScheduleService(ctx).status()
        typer.echo(f"\nScheduler: {status.state}. {status.message}")

    @schedules.command("show")
    def show(ref: Annotated[str, typer.Argument(help="Schedule name or id")]) -> None:
        """Show one schedule and its recent history."""
        ctx = get_context()
        service = ScheduleService(ctx)
        sid = call(lambda: _resolve(ctx, ref))
        s = call(lambda: service.get(sid))
        echo_json(s.model_dump(mode="json"))
        history = service.history(sid, limit=20)
        rows = [
            [_when(o.occurrence_local), o.status, (o.run_id or "-")[:8], o.reason or ""]
            for o in history.items
        ]
        if rows:
            typer.echo("")
            print_table(rows, ["OCCURRENCE (LOCAL)", "STATUS", "RUN", "NOTE"])

    @schedules.command("run-now")
    def run_now(
        ref: Annotated[str, typer.Argument(help="Schedule name or id")],
        force: Annotated[
            bool, typer.Option("--force", help="Ignore the concurrency policy")
        ] = False,
    ) -> None:
        """Create an occurrence now (a worker executes the run)."""
        ctx = get_context()
        sid = call(lambda: _resolve(ctx, ref))
        result = call(lambda: ScheduleService(ctx).run_now(sid, force=force, actor="cli"))
        typer.echo(f"{result.message}" + (f": run {result.run_id}" if result.run_id else ""))

    @schedules.command("pause")
    def pause(ref: Annotated[str, typer.Argument(help="Schedule name or id")]) -> None:
        """Pause a schedule (no new occurrences until resumed)."""
        ctx = get_context()
        sid = call(lambda: _resolve(ctx, ref))
        s = call(lambda: ScheduleService(ctx).set_enabled(sid, False, actor="cli"))
        typer.echo(f"Paused '{s.name}'.")

    @schedules.command("resume")
    def resume(ref: Annotated[str, typer.Argument(help="Schedule name or id")]) -> None:
        """Resume a paused schedule (the next run is computed from now)."""
        ctx = get_context()
        sid = call(lambda: _resolve(ctx, ref))
        s = call(lambda: ScheduleService(ctx).set_enabled(sid, True, actor="cli"))
        typer.echo(f"Resumed '{s.name}'. Next run: {_when(s.next_run_local)} ({s.timezone}).")

    @schedules.command("preview")
    def preview(
        timezone: Annotated[str, typer.Option("--tz", help="IANA time zone")],
        cron: Annotated[str | None, typer.Option("--cron", help="5-field cron")] = None,
        every: Annotated[int | None, typer.Option("--every", help="Interval seconds")] = None,
        count: Annotated[int, typer.Option("--count", "-n", min=1, max=50)] = 5,
    ) -> None:
        """Preview the next run times for a cron expression or interval."""
        ctx = get_context()
        kind: Literal["cron", "interval"] = "cron" if cron else "interval"
        result = ScheduleService(ctx).preview(
            SchedulePreviewRequest(
                kind=kind,
                timezone=timezone,
                cron_expression=cron,
                interval_seconds=every,
                count=count,
            )
        )
        if not result.valid:
            typer.secho(f"error: {result.error}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
        for item in result.occurrences:
            typer.echo(
                f"{item.local}  ({item.utc_offset})" + (f"  {item.note}" if item.note else "")
            )
