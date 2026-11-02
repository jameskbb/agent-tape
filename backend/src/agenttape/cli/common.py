"""Helpers shared by CLI commands: context building, error handling, output, following runs."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any, TypeVar

import click
import typer
from sqlalchemy import select

from agenttape.application.context import AppContext
from agenttape.application.schemas.runs import EventOut, RunOut
from agenttape.application.services.runs import RunService
from agenttape.bootstrap import build_context
from agenttape.config import load_settings
from agenttape.domain.enums import TERMINAL_STATUSES, RunStatus
from agenttape.domain.errors import AgentTapeError
from agenttape.infrastructure.db.models import WorkerHeartbeat

T = TypeVar("T")

STATUS_COLORS = {
    "succeeded": typer.colors.GREEN,
    "failed": typer.colors.RED,
    "timed_out": typer.colors.RED,
    "cancelled": typer.colors.YELLOW,
    "cancelling": typer.colors.YELLOW,
    "waiting_for_approval": typer.colors.MAGENTA,
    "running": typer.colors.CYAN,
    "preparing": typer.colors.CYAN,
    "queued": typer.colors.BLUE,
}


def get_context(*, migrate: bool = True) -> AppContext:
    return build_context(load_settings(), migrate=migrate)


@contextmanager
def cli_errors() -> Iterator[None]:
    """Turn application errors into a clean message and exit code 1."""
    try:
        yield
    except AgentTapeError as exc:
        typer.secho(f"error: {exc.message}", fg=typer.colors.RED, err=True)
        if exc.details:
            typer.echo(json.dumps(exc.details, indent=2, default=str), err=True)
        raise typer.Exit(1) from None


def call(fn: Callable[[], T]) -> T:
    with cli_errors():
        return fn()


def echo_json(value: Any) -> None:
    typer.echo(json.dumps(value, indent=2, default=str))


def status_text(status: str) -> str:
    return typer.style(status, fg=STATUS_COLORS.get(status), bold=True)


def print_table(rows: list[list[str]], headers: list[str]) -> None:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(click.unstyle(cell)))
    typer.echo("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    typer.echo("  ".join("-" * w for w in widths))
    for row in rows:
        typer.echo(
            "  ".join(
                cell + " " * (widths[i] - len(click.unstyle(cell))) for i, cell in enumerate(row)
            )
        )


def format_event(e: EventOut) -> str:
    ts = e.ts.strftime("%H:%M:%S")
    label = e.type.ljust(17)
    color = {
        "error": typer.colors.RED,
        "critical": typer.colors.RED,
        "warning": typer.colors.YELLOW,
        "notice": typer.colors.MAGENTA,
    }.get(e.severity)
    summary = e.summary.replace("\n", " ")
    return f"{ts} #{e.sequence:<4} {typer.style(label, fg=color)} {summary}"


def live_worker_present(ctx: AppContext) -> bool:
    cutoff = ctx.clock.now() - timedelta(
        seconds=max(10.0, ctx.settings.heartbeat_interval_seconds * 3)
    )
    with ctx.session() as session:
        return (
            session.scalars(
                select(WorkerHeartbeat).where(
                    WorkerHeartbeat.status == "running", WorkerHeartbeat.last_heartbeat_at >= cutoff
                )
            ).first()
            is not None
        )


async def follow_run(
    ctx: AppContext, run_id: str, *, stop_on_wait: bool = True, poll: float = 0.4
) -> RunOut:
    """Print events as they arrive until the run is terminal (or waits for approval)."""
    service = RunService(ctx)
    after = 0
    while True:
        page = await asyncio.to_thread(service.list_events, run_id, after_sequence=after, limit=500)
        for e in page.items:
            typer.echo(format_event(e))
            after = e.sequence
        if page.next_after_sequence is not None:
            continue
        run = await asyncio.to_thread(service.get_run, run_id)
        status = RunStatus(run.status)
        if status in TERMINAL_STATUSES or (
            stop_on_wait and status == RunStatus.WAITING_FOR_APPROVAL
        ):
            final = await asyncio.to_thread(
                service.list_events, run_id, after_sequence=after, limit=500
            )
            for e in final.items:
                typer.echo(format_event(e))
            return run
        await asyncio.sleep(poll)


async def execute_inline(ctx: AppContext, run_id: str, *, follow: bool = True) -> RunOut:
    """Run an in-process worker until ``run_id`` settles (used when no worker is running)."""
    from agenttape.worker.worker import Worker

    stop = asyncio.Event()
    worker = Worker(ctx, kind="cli-inline", concurrency=1, poll_interval=0.2)
    worker_task = asyncio.create_task(worker.run(stop))
    try:
        if follow:
            run = await follow_run(ctx, run_id)
        else:
            service = RunService(ctx)
            while True:
                run = await asyncio.to_thread(service.get_run, run_id)
                if (
                    RunStatus(run.status) in TERMINAL_STATUSES
                    or run.status == RunStatus.WAITING_FOR_APPROVAL
                ):
                    break
                await asyncio.sleep(0.3)
    finally:
        stop.set()
        await worker_task
    return run


def exit_for(run: RunOut) -> None:
    typer.echo(
        f"\nRun {run.id}: {status_text(run.status)}"
        + (f" ({run.failure_category})" if run.failure_category else "")
    )
    if run.status in ("failed", "timed_out", "cancelled"):
        raise typer.Exit(2)
