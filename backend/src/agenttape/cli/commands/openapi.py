"""``agenttape openapi``: write the OpenAPI schema (used to check frontend API types)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Annotated

import typer


def register(app: typer.Typer) -> None:
    @app.command()
    def openapi(
        output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    ) -> None:
        """Print or write the OpenAPI document without starting a server."""
        from agenttape.api.app import create_app
        from agenttape.bootstrap import build_context
        from agenttape.config import load_settings

        with tempfile.TemporaryDirectory() as tmp:
            ctx = build_context(load_settings(data_dir=Path(tmp)), migrate=False)
            schema = create_app(ctx).openapi()
            ctx.close()
        text = json.dumps(schema, indent=2, sort_keys=True)
        if output:
            output.write_text(text + "\n", encoding="utf-8")
        else:
            typer.echo(text)
