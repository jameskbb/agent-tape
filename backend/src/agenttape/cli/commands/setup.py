"""``init``, ``migrate``, ``seed``, ``reset-demo``, and ``token`` commands."""

from __future__ import annotations

import sys
from typing import Annotated

import typer

from agenttape.application.services.demo import DemoService
from agenttape.application.services.workspaces import ensure_default_workspace
from agenttape.cli.common import call, echo_json, get_context
from agenttape.config import load_settings
from agenttape.infrastructure.db.migrate import upgrade
from agenttape.security.tokens import generate_token, hash_token


def register(app: typer.Typer) -> None:
    @app.command()
    def init() -> None:
        """Create the data directory, apply migrations, and create the default workspace."""
        settings = load_settings()
        settings.ensure_dirs()
        ctx = get_context(migrate=True)
        with ctx.session() as session:
            ws = ensure_default_workspace(session, ctx.clock)
            ws_name = ws.name
        typer.echo(f"Data directory : {settings.data_dir}")
        typer.echo(f"Database       : {ctx.engine.url.render_as_string(hide_password=True)}")
        typer.echo(f"Workspace      : {ws_name}")
        typer.echo("Ready. Next: `agenttape demo` or `agenttape serve`.")

    @app.command()
    def migrate() -> None:
        """Apply database migrations (upgrade to all heads)."""
        settings = load_settings()
        settings.ensure_dirs()
        upgrade(settings.resolved_database_url)
        typer.echo("Migrations applied.")

    @app.command()
    def seed(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
        """Create the demo project, Demo Agent profiles, and prompts (idempotent, demo-tagged)."""
        ctx = get_context()
        report = call(DemoService(ctx).seed)
        if as_json:
            echo_json({"created": report.created, "existing": report.existing})
            return
        for kind, names in report.created.items():
            typer.echo(f"created {kind}: {', '.join(names)}")
        if not report.created:
            typer.echo("Demo data already present; nothing to create.")

    @app.command("reset-demo")
    def reset_demo(
        yes: Annotated[
            bool, typer.Option("--yes", "-y", help="Do not ask for confirmation")
        ] = False,
    ) -> None:
        """Delete demo-tagged records only (never touches your own projects, prompts, or runs)."""
        if not yes:
            typer.confirm("Delete all demo-tagged records and their artifacts?", abort=True)
        ctx = get_context()
        deleted = call(lambda: DemoService(ctx).reset(actor="cli"))
        if not deleted:
            typer.echo("No demo records found.")
        for table, count in deleted.items():
            typer.echo(f"deleted {count:>4} from {table}")

    token_app = typer.Typer(
        help="Local API token management (only hashes are stored).", no_args_is_help=True
    )
    app.add_typer(token_app, name="token")

    @token_app.command("create")
    def token_create(
        force: Annotated[bool, typer.Option("--force", help="Replace an existing token")] = False,
    ) -> None:
        """Generate a token, store its SHA-256 hash in the data dir, print the token once."""
        settings = load_settings()
        settings.ensure_dirs()
        if settings.token_file.exists() and not force:
            typer.secho(
                "A token already exists. Use --force to replace it.",
                fg=typer.colors.YELLOW,
                err=True,
            )
            raise typer.Exit(1)
        token = generate_token()
        settings.token_file.write_text(hash_token(token) + "\n", encoding="utf-8")
        settings.token_file.chmod(0o600)
        typer.echo(token)
        typer.echo(
            f"Stored hash in {settings.token_file}. Send it as 'Authorization: Bearer <token>'. "
            "It is shown only once.",
            err=True,
        )

    @token_app.command("hash")
    def token_hash() -> None:
        """Read a token from stdin and print the value for AGENTTAPE_API_TOKEN_HASH."""
        token = sys.stdin.readline().strip()
        if not token:
            typer.secho("No token on stdin.", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
        typer.echo(hash_token(token))
