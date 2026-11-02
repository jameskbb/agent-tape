"""``runs replay``, ``runs fork``, and ``runs compare``.

Added to the existing ``runs`` command group (registered by ``runs.py``, which sorts
before this module). They call the same services as the API.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer

from agenttape.application.context import AppContext
from agenttape.application.schemas.git import ForkRequest, LineageCreateResult, ReplayRequest
from agenttape.application.services.comparison import ComparisonService
from agenttape.application.services.lineage import LineageService
from agenttape.application.services.runs import RunService
from agenttape.cli.commands.runs import _parse_inputs, _wait
from agenttape.cli.common import call, echo_json, get_context, print_table, status_text


def full_run_id(ctx: AppContext, ref: str) -> str:
    """Accept a unique run id prefix, like ``runs show``."""
    page = RunService(ctx).list_runs(search=ref, limit=2)
    matches = [r.id for r in page.items if r.id.startswith(ref)]
    return matches[0] if len(matches) == 1 else ref


def _runs_group(app: typer.Typer) -> typer.Typer:
    for info in app.registered_groups:
        if info.name == "runs" and info.typer_instance is not None:
            return info.typer_instance
    group = typer.Typer(help="Inspect and control runs.", no_args_is_help=True)
    app.add_typer(group, name="runs")
    return group


def _report(result: LineageCreateResult, verb: str) -> None:
    run = result.result.run
    typer.echo(f"{verb} run {run.id} [{status_text(run.status)}] {run.title}")
    for change in result.changes:
        typer.echo(f"  change   {change['field']}: {change.get('from')!r} -> {change.get('to')!r}")
    for item in result.drift:
        typer.secho(f"  warning  {item.message}", fg=typer.colors.YELLOW)


def _fmt(value: Any) -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, float):
        return f"{value:.0f}"
    return str(value)


def _ms(value: Any) -> str:
    return f"{value / 1000:.1f}s" if isinstance(value, int | float) else "unavailable"


def register(app: typer.Typer) -> None:
    runs_app = _runs_group(app)

    @runs_app.command("replay")
    def replay(
        run_id: str,
        title: Annotated[str | None, typer.Option(help="Title for the new run")] = None,
        follow: Annotated[
            bool, typer.Option("--follow", "-f", help="Stream events until the new run settles")
        ] = False,
        inline: Annotated[
            bool | None, typer.Option("--inline/--no-inline", help="Execute in this process")
        ] = None,
    ) -> None:
        """Replay a run with exactly the same versions and inputs."""
        ctx = get_context()
        result = call(
            lambda: LineageService(ctx).replay(
                full_run_id(ctx, run_id), ReplayRequest(title=title), actor="cli"
            )
        )
        _report(result, "Replay")
        if follow:
            _wait(ctx, result.result.run.id, follow=True, inline=inline)

    @runs_app.command("fork")
    def fork(
        run_id: str,
        input_: Annotated[
            list[str] | None, typer.Option("--input", "-i", help="Changed variable key=value")
        ] = None,
        prompt_version: Annotated[
            str | None, typer.Option("--prompt-version", help="Prompt version id")
        ] = None,
        profile: Annotated[str | None, typer.Option("--profile", help="Agent profile id")] = None,
        profile_version: Annotated[
            str | None, typer.Option("--profile-version", help="Agent profile version id")
        ] = None,
        title: Annotated[str | None, typer.Option(help="Title for the new run")] = None,
        follow: Annotated[
            bool, typer.Option("--follow", "-f", help="Stream events until the new run settles")
        ] = False,
        inline: Annotated[
            bool | None, typer.Option("--inline/--no-inline", help="Execute in this process")
        ] = None,
    ) -> None:
        """Fork a run with a changed prompt version, agent profile, or variables."""
        ctx = get_context()
        body = ForkRequest(
            title=title,
            prompt_version_id=prompt_version,
            agent_profile_id=profile,
            agent_profile_version_id=profile_version,
            inputs=_parse_inputs(input_ or []),
        )
        result = call(lambda: LineageService(ctx).fork(full_run_id(ctx, run_id), body, actor="cli"))
        _report(result, "Fork")
        if follow:
            _wait(ctx, result.result.run.id, follow=True, inline=inline)

    @runs_app.command("compare")
    def compare(
        run_a: str,
        run_b: str,
        as_json: Annotated[bool, typer.Option("--json")] = False,
    ) -> None:
        """Compare two runs side by side."""
        ctx = get_context()
        cmp = call(
            lambda: ComparisonService(ctx).compare(full_run_id(ctx, run_a), full_run_id(ctx, run_b))
        )
        if as_json:
            echo_json(cmp.model_dump(mode="json"))
            return
        a, b, d = cmp.a, cmp.b, cmp.differences
        rows = [
            ["Title", a.run.title[:40], b.run.title[:40]],
            ["Status", a.run.status, b.run.status],
            ["Duration", _ms(a.run.duration_ms), _ms(b.run.duration_ms)],
            ["Attempts", str(a.attempts), str(b.attempts)],
            ["Events", str(a.event_count), str(b.event_count)],
            ["Usage", a.usage_status, b.usage_status],
            [
                "Prompt",
                f"{(a.prompt or {}).get('name')} v{(a.prompt or {}).get('version')}",
                f"{(b.prompt or {}).get('name')} v{(b.prompt or {}).get('version')}",
            ],
            [
                "Profile",
                f"{(a.profile or {}).get('name')} v{(a.profile or {}).get('version')}",
                f"{(b.profile or {}).get('name')} v{(b.profile or {}).get('version')}",
            ],
            [
                "Git changes",
                f"{a.git['files']} files +{a.git['insertions']} -{a.git['deletions']}",
                f"{b.git['files']} files +{b.git['insertions']} -{b.git['deletions']}",
            ],
        ]
        for row in d["usage"]["rows"]:
            rows.append([f"  {row['key']}", _fmt(row["a"]), _fmt(row["b"])])
        print_table(rows, ["", f"A {a.run.id[:8]}", f"B {b.run.id[:8]}"])
        if cmp.relation:
            typer.echo(f"\nRelation: {cmp.relation.replace('_', ' ')}")
        if d["inputs"]:
            typer.echo("\nInputs that differ:")
            for item in d["inputs"]:
                typer.echo(f"  {item['key']}: {json.dumps(item['a'])} -> {json.dumps(item['b'])}")
        changed_types = [t for t in d["event_types"] if t["delta"]]
        if changed_types:
            typer.echo("\nEvent types that differ:")
            for t in changed_types:
                typer.echo(f"  {t['key']:<20} {t['a']:>4} -> {t['b']:>4} ({t['delta']:+d})")
        changed_evals = [e for e in d["evaluations"] if e["changed"]]
        if changed_evals:
            typer.echo("\nEvaluations that differ:")
            for e in changed_evals:
                typer.echo(f"  {e['name']}: {e['a_status'] or '-'} -> {e['b_status'] or '-'}")
