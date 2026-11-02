"""``agenttape`` command-line entry point.

Subcommands are auto-registered: every module in ``agenttape/cli/commands/`` exposing
``register(app: typer.Typer) -> None`` is imported and asked to add its commands. Later
phases add a file there (e.g. ``schedules.py``) instead of editing this module.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Annotated

import typer

from agenttape import __version__
from agenttape.plugins import import_submodules

COMMANDS_PACKAGE = "agenttape.cli.commands"

app = typer.Typer(
    name="agenttape",
    help="AgentTape: local-first control plane and flight recorder for AI coding agents.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)


def _version(value: bool) -> None:
    if value:
        typer.echo(f"agenttape {__version__}")
        raise typer.Exit()


@app.callback()
def main_callback(
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir", envvar="AGENTTAPE_DATA_DIR", help="Data directory (default ~/.agenttape)"
        ),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging")] = False,
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version, is_eager=True, help="Show version and exit"),
    ] = False,
) -> None:
    if data_dir is not None:
        os.environ["AGENTTAPE_DATA_DIR"] = str(data_dir.expanduser())
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def register_commands(target: typer.Typer) -> None:
    for module in import_submodules(COMMANDS_PACKAGE):
        register = getattr(module, "register", None)
        if callable(register):
            register(target)


register_commands(app)


def main() -> None:
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
