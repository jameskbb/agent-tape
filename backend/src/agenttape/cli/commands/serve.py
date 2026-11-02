"""``agenttape serve`` (API + UI + background services) and ``agenttape worker``."""

from __future__ import annotations

import asyncio
import signal
from typing import Annotated

import typer
import uvicorn

from agenttape.api.app import create_app
from agenttape.cli.common import get_context
from agenttape.config import is_loopback_host, load_settings
from agenttape.runtime.base import available_services
from agenttape.worker.worker import Worker


def register(app: typer.Typer) -> None:
    @app.command()
    def serve(
        host: Annotated[
            str | None, typer.Option(help="Bind address (default AGENTTAPE_HOST or 127.0.0.1)")
        ] = None,
        port: Annotated[
            int | None, typer.Option(help="Port (default AGENTTAPE_PORT or 8787)")
        ] = None,
        no_worker: Annotated[
            bool, typer.Option("--no-worker", help="Do not run the in-process worker")
        ] = False,
        no_scheduler: Annotated[
            bool,
            typer.Option(
                "--no-scheduler", help="Do not run the in-process scheduler (when installed)"
            ),
        ] = False,
        without: Annotated[
            list[str] | None, typer.Option("--without", help="Disable a background service by name")
        ] = None,
        no_migrate: Annotated[
            bool, typer.Option("--no-migrate", help="Skip applying migrations")
        ] = False,
    ) -> None:
        """Serve the API and web UI, plus the worker and any other registered services."""
        settings = load_settings()
        bind_host = host or settings.host
        bind_port = port or settings.port
        if not is_loopback_host(bind_host) and settings.effective_token_hash() is None:
            typer.secho(
                f"Refusing to bind to {bind_host}: it is reachable beyond this machine and no API token "
                "is configured. Run `agenttape token create` (or set AGENTTAPE_API_TOKEN_HASH) first.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(1)
        if bind_host != settings.host or bind_port != settings.port:
            settings = settings.model_copy(update={"host": bind_host, "port": bind_port})
        from agenttape.bootstrap import build_context

        ctx = build_context(settings, migrate=not no_migrate)
        disabled = set(without or [])
        if no_worker:
            disabled.add("worker")
        if no_scheduler:
            disabled.add("scheduler")
        services = [
            name
            for name, spec in available_services().items()
            if spec.default_enabled and name not in disabled
        ]
        application = create_app(ctx, services=services)
        typer.echo(
            f"AgentTape on http://{bind_host}:{bind_port}  (services: {', '.join(services) or 'none'})"
        )
        uvicorn.run(
            application,
            host=bind_host,
            port=bind_port,
            log_level=settings.log_level,
            access_log=False,
        )

    @app.command()
    def worker(
        concurrency: Annotated[
            int | None, typer.Option(help="Parallel runs (default AGENTTAPE_WORKER_CONCURRENCY)")
        ] = None,
    ) -> None:
        """Run a standalone worker process (claims and executes queued runs)."""
        ctx = get_context()
        w = Worker(ctx, concurrency=concurrency, kind="worker")
        typer.echo(
            f"Worker {w.worker_id} running with concurrency {w.concurrency}. Ctrl+C to stop."
        )

        async def main() -> None:
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop.set)
            await w.run(stop)

        asyncio.run(main())
