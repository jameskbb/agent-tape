"""``git status|commit|cleanup``: a run's changes, approved commits, owned-worktree cleanup."""

from __future__ import annotations

from typing import Annotated

import typer

from agenttape.application.services.git import GitService
from agenttape.cli.commands.runs_lineage import full_run_id
from agenttape.cli.common import call, echo_json, get_context, print_table


def register(app: typer.Typer) -> None:
    git_app = typer.Typer(
        help="Git isolation: a run's changes, approved local commits, worktree cleanup.",
        no_args_is_help=True,
    )
    app.add_typer(git_app, name="git")

    @git_app.command("status")
    def status(run_id: str, as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
        """Starting revision, worktree, changed files, validations, and commit state."""
        ctx = get_context()
        view = call(lambda: GitService(ctx).view(full_run_id(ctx, run_id)))
        if as_json:
            echo_json(view.model_dump(mode="json"))
            return
        start = view.starting
        if start and start.is_repo:
            typer.echo(f"Started from {start.head or '?'} on {start.branch or 'detached HEAD'}")
            if start.dirty:
                typer.secho(
                    f"  warning: {start.dirty_count} uncommitted file(s) at start",
                    fg=typer.colors.YELLOW,
                )
        ws = view.workspace
        if ws and ws.isolated:
            typer.echo(f"Worktree {ws.path} on {ws.branch} [{ws.status}]")
            if ws.uncommitted_count is not None:
                typer.echo(f"  {ws.uncommitted_count} uncommitted change(s)")
        s = view.summary
        typer.echo(
            f"{s.files} file(s) changed, +{s.insertions} -{s.deletions}"
            + (f", {s.protected_files} protected" if s.protected_files else "")
        )
        rows = [
            [
                f.change_type,
                f.path + (f" (from {f.old_path})" if f.old_path else ""),
                "binary" if f.is_binary else f"+{f.insertions or 0} -{f.deletions or 0}",
                "PROTECTED" if f.is_protected else "",
            ]
            for f in view.files
        ]
        if rows:
            print_table(rows, ["CHANGE", "PATH", "LINES", ""])
        for v in view.validations:
            typer.echo(f"validation  {v.status:<7} {v.name}")
        c = view.commit
        if c.pending:
            typer.echo(f"Commit waiting for approval: {c.pending.id}")
        elif not c.available and c.reason:
            typer.echo(f"Commit: {c.reason}")

    @git_app.command("commit")
    def commit(
        run_id: str,
        message: Annotated[str, typer.Option("--message", "-m", help="Commit message")],
    ) -> None:
        """Request a local commit in the run's worktree. It happens only after approval."""
        ctx = get_context()
        approval = call(
            lambda: GitService(ctx).request_commit(
                full_run_id(ctx, run_id), message=message, actor="cli"
            )
        )
        typer.echo(
            f"Commit requested; approval {approval.id} is pending. Approve it in the web UI or "
            f"with the approvals API. Nothing is committed until then."
        )

    @git_app.command("cleanup")
    def cleanup(run_id: str) -> None:
        """Remove the run's AgentTape-owned worktree (refused if work would be lost)."""
        ctx = get_context()
        out = call(lambda: GitService(ctx).cleanup(full_run_id(ctx, run_id), actor="cli"))
        if out.removed:
            typer.echo(f"Removed {out.path}")
            if out.branch_deleted:
                typer.echo(f"Deleted branch {out.branch} (it had no commits)")
            elif out.branch_kept_reason:
                typer.echo(f"Branch {out.branch} {out.branch_kept_reason}")
            return
        typer.secho(out.message, fg=typer.colors.YELLOW, err=True)
        for path in out.uncommitted[:20]:
            typer.echo(f"  {path}", err=True)
        raise typer.Exit(1)
