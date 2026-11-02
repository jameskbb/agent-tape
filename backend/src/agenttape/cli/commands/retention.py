"""``retention show|set|preview|apply``: data retention with a dry-run preview first."""

from __future__ import annotations

from typing import Annotated

import typer

from agenttape.application.schemas.git import RetentionExecute
from agenttape.application.services.retention import RetentionService
from agenttape.cli.common import call, echo_json, get_context, print_table


def _bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def register(app: typer.Typer) -> None:
    ret = typer.Typer(
        help="Data retention: settings, dry-run preview, cleanup.", no_args_is_help=True
    )
    app.add_typer(ret, name="retention")

    @ret.command("show")
    def show() -> None:
        """Print the retention settings."""
        ctx = get_context()
        echo_json(call(RetentionService(ctx).get_settings).model_dump())

    @ret.command("set")
    def set_(
        max_age_days: Annotated[int | None, typer.Option(min=1, max=3650)] = None,
        include_demo: Annotated[bool | None, typer.Option("--include-demo/--exclude-demo")] = None,
        remove_worktrees: Annotated[
            bool | None, typer.Option("--remove-worktrees/--keep-worktrees")
        ] = None,
        keep_tag: Annotated[str | None, typer.Option(help="Runs with this tag are kept")] = None,
    ) -> None:
        """Change retention settings (only the options you pass)."""
        ctx = get_context()
        service = RetentionService(ctx)
        current = call(service.get_settings)
        updates = {
            k: v
            for k, v in {
                "max_age_days": max_age_days,
                "include_demo": include_demo,
                "remove_worktrees": remove_worktrees,
                "keep_tag": keep_tag,
            }.items()
            if v is not None
        }
        saved = call(
            lambda: service.update_settings(current.model_copy(update=updates), actor="cli")
        )
        echo_json(saved.model_dump())

    @ret.command("preview")
    def preview(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
        """Dry run: list what cleanup would delete. Nothing is deleted."""
        ctx = get_context()
        plan = call(RetentionService(ctx).preview)
        if as_json:
            echo_json(plan.model_dump(mode="json"))
            return
        typer.echo(f"Runs finished before {plan.cutoff:%Y-%m-%d %H:%M} UTC")
        rows = [
            [i.run_id[:8], i.status, _bytes(i.total_bytes), i.blocked or "delete", i.title[:50]]
            for i in plan.runs
        ]
        if rows:
            print_table(rows, ["RUN", "STATUS", "SIZE", "ACTION", "TITLE"])
        for o in plan.orphans:
            typer.echo(
                f"orphan {o.kind:<10} {_bytes(o.bytes):>9}  {o.blocked or 'delete'}  {o.path}"
            )
        typer.echo(
            f"\n{plan.total_runs} run(s) and {sum(1 for o in plan.orphans if not o.blocked)} "
            f"orphan(s) would be deleted, freeing {_bytes(plan.total_bytes)}; "
            f"{plan.blocked_runs} run(s) blocked."
        )

    @ret.command("apply")
    def apply(
        yes: Annotated[
            bool, typer.Option("--yes", "-y", help="Do not ask for confirmation")
        ] = False,
    ) -> None:
        """Delete what the preview lists (re-checked at execution; AgentTape data only)."""
        ctx = get_context()
        service = RetentionService(ctx)
        plan = call(service.preview)
        run_ids = [i.run_id for i in plan.runs if not i.blocked]
        orphans = [o.path for o in plan.orphans if not o.blocked]
        if not run_ids and not orphans:
            typer.echo("Nothing to clean up.")
            return
        if not yes:
            typer.confirm(
                f"Delete {len(run_ids)} run(s) and {len(orphans)} orphaned director(ies), "
                f"about {_bytes(plan.total_bytes)}?",
                abort=True,
            )
        result = call(
            lambda: service.execute(
                RetentionExecute(run_ids=run_ids, orphan_paths=orphans), actor="cli"
            )
        )
        typer.echo(
            f"Deleted {len(result.deleted_runs)} run(s), removed {len(result.removed_paths)} "
            f"path(s), freed about {_bytes(result.freed_bytes)}."
        )
        for s in result.skipped:
            typer.secho(f"  skipped {s.get('run_id') or s.get('path')}: {s['reason']}", fg="yellow")
