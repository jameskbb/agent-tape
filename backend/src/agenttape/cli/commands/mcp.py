"""``agenttape mcp serve|register|unregister|status``: the AgentTape MCP server for Claude
Code, and the one CLI command that registers it for a user's interactive sessions.

``register``/``unregister`` only ever run ``claude`` with an explicit argument list (never
a shell), and never put a secret in that argv or in Claude Code's own config - the
per-machine ``mcp`` client token reaches the server through a file in the data dir, found
via ``AGENTTAPE_DATA_DIR``.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Annotated

import typer

import agenttape.mcp.server as mcp_server
from agenttape.cli.common import echo_json
from agenttape.config import load_settings
from agenttape.mcp.launch import RECORD_FINDING_TOOL, SERVER_NAME
from agenttape.security.capture import CaptureTokenStore, capture_api_base
from agenttape.security.redaction import DEFAULT_REDACTOR

CLAUDE_EXECUTABLE_ENV = "AGENTTAPE_CLAUDE_EXECUTABLE"
DEFAULT_CLAUDE_NAME = "claude"
_NOT_FOUND_MESSAGE = (
    f"Claude Code (`{DEFAULT_CLAUDE_NAME}`) was not found; install it or set "
    f"{CLAUDE_EXECUTABLE_ENV}."
)


def _resolve_claude_executable() -> str | None:
    override = os.environ.get(CLAUDE_EXECUTABLE_ENV)
    if override:
        return override if Path(override).is_file() else None
    return shutil.which(DEFAULT_CLAUDE_NAME)


def _remove_argv(claude: str) -> list[str]:
    return [claude, "mcp", "remove", "--scope", "user", SERVER_NAME]


def _add_argv(claude: str, *, data_dir: Path, api_base: str) -> list[str]:
    return [
        claude,
        "mcp",
        "add",
        "--scope",
        "user",
        SERVER_NAME,
        "-e",
        f"AGENTTAPE_DATA_DIR={data_dir}",
        "-e",
        f"AGENTTAPE_API_URL={api_base}",
        "--",
        sys.executable,
        "-m",
        "agenttape.cli.main",
        "mcp",
        "serve",
    ]


def _registration() -> tuple[bool | None, list[str]]:
    """Whether ``claude mcp get agenttape`` finds the server: ``None`` with a warning when
    Claude Code is not installed. Its output is never echoed."""
    claude = _resolve_claude_executable()
    if claude is None:
        return None, ["Claude Code was not found, so the registration could not be checked."]
    try:
        proc = subprocess.run(
            [claude, "mcp", "get", SERVER_NAME],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, ["`claude mcp get` did not run, so the registration could not be checked."]
    return proc.returncode == 0, []


def register(app: typer.Typer) -> None:
    mcp_app = typer.Typer(help="AgentTape MCP server for Claude Code", no_args_is_help=True)
    app.add_typer(mcp_app, name="mcp")

    @mcp_app.command()
    def serve() -> None:
        """Serve the AgentTape MCP server over stdio (Claude Code starts this; you do not
        run it by hand)."""
        mcp_server.main()

    @mcp_app.command(name="register")
    def register_cmd(
        rotate: Annotated[
            bool,
            typer.Option(
                "--rotate",
                help="Replace the mcp client token even if the existing one is still valid",
            ),
        ] = False,
        print_command: Annotated[
            bool,
            typer.Option(
                "--print-command",
                help=(
                    "Print the `claude mcp add` command register would run; runs nothing and "
                    "mints no token"
                ),
            ),
        ] = False,
    ) -> None:
        """Register the AgentTape MCP server for every interactive Claude Code session on
        this machine (user scope, all projects) with one command."""
        claude = _resolve_claude_executable()
        if claude is None:
            typer.secho(f"error: {_NOT_FOUND_MESSAGE}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)

        settings = load_settings()
        add_argv = _add_argv(
            claude, data_dir=settings.data_dir, api_base=capture_api_base(settings)
        )
        if print_command:
            # Before any token store access: printing the command mints and reads nothing.
            typer.echo(shlex.join(add_argv))
            return

        settings.ensure_dirs()
        store = CaptureTokenStore(settings.data_dir)
        current = store.read_client_token("mcp")
        reusable = not rotate and current is not None and store.verify("mcp", current)
        minted_here = False
        if not reusable:
            store.mint("mcp", client_copy=True)
            minted_here = True

        subprocess.run(
            _remove_argv(claude), capture_output=True, text=True, timeout=60, check=False
        )
        proc = subprocess.run(add_argv, capture_output=True, text=True, timeout=60, check=False)
        if proc.returncode != 0:
            if minted_here:
                store.revoke("mcp")
            stderr = DEFAULT_REDACTOR.redact_text((proc.stderr or proc.stdout or "").strip())
            typer.secho(f"error: claude mcp add failed: {stderr}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)

        typer.echo("Registered the AgentTape MCP server for all projects (user scope).")
        typer.echo("The server needs `agenttape serve` running.")
        typer.echo(
            f"Allow `{RECORD_FINDING_TOOL}` when Claude Code asks (or add it to permissions.allow)."
        )
        typer.echo("Findings are recorded only from directories inside a registered project.")
        typer.echo("With `agenttape hooks setup`, findings join the session's run.")

    @mcp_app.command(name="unregister")
    def unregister_cmd() -> None:
        """Remove the AgentTape MCP server registration for interactive sessions and
        revoke its per-machine token."""
        claude = _resolve_claude_executable()
        if claude is None:
            typer.secho(f"error: {_NOT_FOUND_MESSAGE}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)

        proc = subprocess.run(
            _remove_argv(claude), capture_output=True, text=True, timeout=60, check=False
        )
        if proc.returncode == 0:
            typer.echo("Removed the AgentTape MCP server from Claude Code (user scope).")
        else:
            stderr = DEFAULT_REDACTOR.redact_text((proc.stderr or proc.stdout or "").strip())
            typer.echo(f"claude mcp remove reported: {stderr}")

        settings = load_settings()
        if CaptureTokenStore(settings.data_dir).revoke("mcp"):
            typer.echo("Revoked the mcp token.")
        else:
            typer.echo("No mcp token was present.")

    @mcp_app.command(name="status")
    def status_cmd(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
        """Report whether Claude Code has the AgentTape MCP server registered and whether
        this machine's mcp client token is valid."""
        settings = load_settings()
        store = CaptureTokenStore(settings.data_dir)
        token = store.read_client_token("mcp")
        token_valid = token is not None and store.verify("mcp", token)
        registered, warnings = _registration()

        if as_json:
            echo_json({"registered": registered, "token_valid": token_valid, "warnings": warnings})
            return

        answer = {True: "yes", False: "no", None: "unknown"}[registered]
        typer.echo(f"Registered with Claude Code: {answer}")
        typer.echo(f"Token valid: {token_valid}")
        for warning in warnings:
            typer.secho(f"warning: {warning}", fg=typer.colors.YELLOW, err=True)
