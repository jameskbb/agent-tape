"""``run``, ``runs list|show|cancel|retry``, ``demo``, and ``export`` commands.

All of them call the same application services as the HTTP API.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any

import typer
from sqlalchemy import or_, select

from agenttape.application.context import AppContext
from agenttape.application.schemas.runs import RunCreate
from agenttape.application.services.demo import DemoService
from agenttape.application.services.exports import ExportService
from agenttape.application.services.runs import RunService
from agenttape.cli.common import (
    call,
    echo_json,
    execute_inline,
    exit_for,
    follow_run,
    get_context,
    live_worker_present,
    print_table,
    status_text,
)
from agenttape.domain.enums import RunSource
from agenttape.domain.errors import NotFoundError
from agenttape.infrastructure.db.models import AgentProfile, Project, PromptTemplate
from agenttape.seeds.core_demo import DEMO_PROFILES


def _resolve(ctx: AppContext, model: Any, ref: str, label: str) -> str:
    with ctx.session() as session:
        row = session.scalars(select(model).where(or_(model.id == ref, model.name == ref))).first()
        if row is None:
            raise NotFoundError(label, ref)
        return str(row.id)


def _parse_inputs(pairs: list[str]) -> dict[str, Any]:
    inputs: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            raise typer.BadParameter(f"expected key=value, got '{pair}'")
        key, value = pair.split("=", 1)
        try:
            inputs[key] = json.loads(value)
        except json.JSONDecodeError:
            inputs[key] = value
    return inputs


def _wait(ctx: AppContext, run_id: str, *, follow: bool, inline: bool | None) -> None:
    use_inline = inline if inline is not None else not live_worker_present(ctx)
    if use_inline:
        typer.echo("No running worker detected; executing in this process.", err=True)
        run = asyncio.run(execute_inline(ctx, run_id, follow=follow))
    else:
        run = asyncio.run(follow_run(ctx, run_id))
    exit_for(run)


def register(app: typer.Typer) -> None:
    @app.command()
    def run(
        profile: Annotated[str, typer.Option("--profile", "-p", help="Agent profile name or id")],
        prompt_template: Annotated[
            str | None, typer.Option("--prompt-template", "-t", help="Prompt template name or id")
        ] = None,
        prompt: Annotated[str | None, typer.Option("--prompt", help="Inline prompt text")] = None,
        input_: Annotated[
            list[str] | None, typer.Option("--input", "-i", help="Variable as key=value")
        ] = None,
        project: Annotated[str | None, typer.Option("--project", help="Project name or id")] = None,
        title: Annotated[str | None, typer.Option(help="Run title")] = None,
        idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
        priority: Annotated[int, typer.Option(help="Queue priority (-100..100)")] = 0,
        follow: Annotated[
            bool, typer.Option("--follow", "-f", help="Stream events until the run settles")
        ] = False,
        inline: Annotated[
            bool | None,
            typer.Option(
                "--inline/--no-inline",
                help="Execute in this process (default: only if no worker runs)",
            ),
        ] = None,
    ) -> None:
        """Start a run through the shared run engine."""
        ctx = get_context()

        def create() -> Any:
            req = RunCreate(
                agent_profile_id=_resolve(ctx, AgentProfile, profile, "agent profile"),
                prompt_template_id=_resolve(ctx, PromptTemplate, prompt_template, "prompt template")
                if prompt_template
                else None,
                prompt=prompt,
                inputs=_parse_inputs(input_ or []),
                project_id=_resolve(ctx, Project, project, "project") if project else None,
                title=title,
                idempotency_key=idempotency_key,
                priority=priority,
                source=RunSource.CLI,
            )
            return RunService(ctx).create_run(req, actor="cli")

        result = call(create)
        verb = "Created" if result.created else "Existing (idempotent)"
        typer.echo(
            f"{verb} run {result.run.id} [{status_text(result.run.status)}] {result.run.title}"
        )
        if follow:
            _wait(ctx, result.run.id, follow=True, inline=inline)

    @app.command()
    def demo(
        scenario: Annotated[
            str, typer.Option(help=f"One of: {', '.join(DEMO_PROFILES)}")
        ] = "success",
        no_run: Annotated[bool, typer.Option("--no-run", help="Only seed demo data")] = False,
        detach: Annotated[
            bool, typer.Option("--detach", help="Start the run and return immediately")
        ] = False,
        inline: Annotated[bool | None, typer.Option("--inline/--no-inline")] = None,
    ) -> None:
        """Seed demo data and start a live Demo Agent run (no credentials needed)."""
        ctx = get_context()
        service = DemoService(ctx)
        report = call(lambda: service.seed(include_runs=False))
        created = sum(len(v) for v in report.created.values())
        typer.echo(f"Demo data ready ({created} records created).")
        if no_run:
            return
        result = call(
            lambda: service.start_run(scenario=scenario, source=RunSource.CLI, actor="cli")
        )
        typer.echo(f"Started demo run {result.run.id} ({DEMO_PROFILES[scenario][0]})")
        if detach:
            return
        _wait(ctx, result.run.id, follow=True, inline=inline)

    runs_app = typer.Typer(help="Inspect and control runs.", no_args_is_help=True)
    app.add_typer(runs_app, name="runs")

    @runs_app.command("list")
    def runs_list(
        status: Annotated[list[str] | None, typer.Option("--status", "-s")] = None,
        limit: Annotated[int, typer.Option(min=1, max=200)] = 20,
        search: Annotated[str | None, typer.Option("--search", "-q")] = None,
        as_json: Annotated[bool, typer.Option("--json")] = False,
    ) -> None:
        """List recent runs."""
        ctx = get_context()
        page = call(lambda: RunService(ctx).list_runs(status=status, search=search, limit=limit))
        if as_json:
            echo_json(page.model_dump(mode="json"))
            return
        rows = [
            [
                r.id[:8],
                status_text(r.status),
                r.source,
                (r.adapter_type or r.kind),
                r.created_at.strftime("%Y-%m-%d %H:%M"),
                f"{r.duration_ms / 1000:.1f}s" if r.duration_ms is not None else "-",
                r.title[:60],
            ]
            for r in page.items
        ]
        print_table(rows, ["ID", "STATUS", "SOURCE", "ADAPTER", "CREATED", "DURATION", "TITLE"])
        typer.echo(f"\n{len(page.items)} of {page.total} runs")

    def _full_id(ctx: AppContext, ref: str) -> str:
        page = RunService(ctx).list_runs(search=ref, limit=2)
        matches = [r.id for r in page.items if r.id.startswith(ref)]
        if len(matches) == 1:
            return matches[0]
        return ref

    @runs_app.command("show")
    def runs_show(
        run_id: str,
        events: Annotated[
            bool, typer.Option("--events", help="Also print the event timeline")
        ] = False,
        as_json: Annotated[bool, typer.Option("--json")] = False,
    ) -> None:
        """Show one run (accepts a unique id prefix)."""
        ctx = get_context()
        detail = call(lambda: RunService(ctx).get_detail(_full_id(ctx, run_id)))
        if as_json:
            echo_json(detail.model_dump(mode="json"))
            return
        r = detail.run
        typer.echo(f"{r.title}\n  id        {r.id}\n  status    {status_text(r.status)}")
        if r.failure_category:
            typer.echo(f"  failure   {r.failure_category}: {r.failure_message}")
        typer.echo(
            f"  source    {r.source}   adapter {r.adapter_type or '-'}   attempts {r.attempt_count}/{r.max_attempts}"
        )
        if r.duration_ms is not None:
            typer.echo(f"  duration  {r.duration_ms / 1000:.2f}s")
        if r.usage:
            label = " (simulated)" if r.usage.get("simulated") else ""
            typer.echo(
                f"  usage     {r.usage.get('input_tokens', '?')} in / {r.usage.get('output_tokens', '?')} out{label}"
            )
        for ev in detail.evaluations:
            typer.echo(f"  eval      {ev.name}: {ev.status}")
        for art in detail.artifacts:
            typer.echo(f"  artifact  {art.name} ({art.size_bytes} bytes)")
        for change in detail.file_changes:
            typer.echo(
                f"  change    {change.change_type:<9} {change.path} (+{change.insertions or 0} -{change.deletions or 0})"
            )
        typer.echo(f"  events    {detail.event_count}")
        if events:
            from agenttape.cli.common import format_event

            page = RunService(ctx).list_events(r.id, limit=1000)
            for e in page.items:
                typer.echo(format_event(e))

    @runs_app.command("cancel")
    def runs_cancel(
        run_id: str,
        reason: Annotated[str | None, typer.Option(help="Reason recorded in the audit log")] = None,
    ) -> None:
        """Cancel a queued, waiting, or running run."""
        ctx = get_context()
        out = call(
            lambda: RunService(ctx).cancel_run(_full_id(ctx, run_id), reason=reason, actor="cli")
        )
        typer.echo(f"Run {out.id}: {status_text(out.status)}")

    @runs_app.command("retry")
    def runs_retry(run_id: str) -> None:
        """Retry a failed, timed-out, or cancelled run (adds an attempt)."""
        ctx = get_context()
        out = call(lambda: RunService(ctx).retry_run(_full_id(ctx, run_id), actor="cli"))
        typer.echo(f"Run {out.id}: {status_text(out.status)}")

    @app.command()
    def export(
        run_id: str,
        output: Annotated[
            Path | None, typer.Option("--output", "-o", help="File to write (default stdout)")
        ] = None,
        as_zip: Annotated[
            bool,
            typer.Option("--zip", help="Write a zip (bundle.json + text artifacts); needs -o"),
        ] = False,
    ) -> None:
        """Export a run as a portable, sanitized JSON bundle (or zip with --zip)."""
        ctx = get_context()
        if as_zip:
            if output is None:
                typer.secho("--zip needs --output FILE", fg=typer.colors.RED, err=True)
                raise typer.Exit(1)
            data = call(lambda: ExportService(ctx).run_bundle_archive(_full_id(ctx, run_id)))
            output.write_bytes(data)
            typer.echo(f"Wrote {output} ({len(data)} bytes, sanitized zip bundle)")
            return
        bundle = call(lambda: ExportService(ctx).run_bundle(_full_id(ctx, run_id)))
        text = json.dumps(bundle, indent=2, default=str)
        if output is None:
            typer.echo(text)
        else:
            output.write_text(text + "\n", encoding="utf-8")
            typer.echo(
                f"Wrote {output} ({len(bundle['events'])} events, {len(bundle['artifacts'])} artifacts)"
            )
