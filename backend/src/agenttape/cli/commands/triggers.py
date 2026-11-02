"""``agenttape triggers list`` (webhook triggers are created in the UI or via the API,
because the token is shown only once)."""

from __future__ import annotations

from typing import Annotated

import typer

from agenttape.application.services.triggers import TriggerService
from agenttape.cli.common import call, echo_json, get_context, print_table


def register(app: typer.Typer) -> None:
    triggers = typer.Typer(help="Inspect webhook triggers.", no_args_is_help=True)
    app.add_typer(triggers, name="triggers")

    @triggers.command("list")
    def list_(as_json: Annotated[bool, typer.Option("--json", help="Print JSON")] = False) -> None:
        """List webhook triggers and their delivery counts."""
        ctx = get_context()
        page = call(lambda: TriggerService(ctx).list_triggers(limit=200))
        if as_json:
            echo_json([t.model_dump(mode="json") for t in page.items])
            return
        if not page.items:
            typer.echo("No triggers yet. Create one in Settings > Webhook triggers.")
            return
        rows = [
            [
                t.id[:8],
                t.name,
                "enabled" if t.enabled else "disabled",
                t.target_name or "-",
                t.webhook_path,
                str(t.delivery_count),
            ]
            for t in page.items
        ]
        print_table(rows, ["ID", "NAME", "STATE", "TARGET", "PATH", "DELIVERIES"])
