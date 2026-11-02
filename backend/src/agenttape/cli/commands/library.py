"""``agenttape prompts|agents|projects|secrets|executables``: the catalog commands.

Every command calls the same application services as the API.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from agenttape.application.schemas.library import PromptExportBundle
from agenttape.application.services.profile_library import ProfileLibraryService
from agenttape.application.services.profiles import AgentProfileService
from agenttape.application.services.project_library import ProjectLibraryService
from agenttape.application.services.projects import ProjectService
from agenttape.application.services.prompt_library import PromptLibraryService
from agenttape.application.services.prompts import PromptService
from agenttape.cli.common import call, echo_json, get_context, print_table

JsonOpt = Annotated[bool, typer.Option("--json", help="Print JSON")]


def register(app: typer.Typer) -> None:
    _prompts(app)
    _agents(app)
    _projects(app)
    _secrets(app)
    _executables(app)


# --------------------------------------------------------------------------- prompts


def _prompts(app: typer.Typer) -> None:
    prompts = typer.Typer(help="Browse, export, and import prompt templates.", no_args_is_help=True)
    app.add_typer(prompts, name="prompts")

    @prompts.command("list")
    def list_(
        q: Annotated[str | None, typer.Option("--search", "-q")] = None,
        tag: Annotated[list[str] | None, typer.Option("--tag")] = None,
        archived: Annotated[bool, typer.Option("--archived", help="Only archived")] = False,
        as_json: JsonOpt = False,
    ) -> None:
        """List prompt templates."""
        ctx = get_context()
        page = call(
            lambda: PromptLibraryService(ctx).search(
                q=q, tags=tag, archived="archived" if archived else "active", limit=500
            )
        )
        if as_json:
            echo_json([t.model_dump(mode="json") for t in page.items])
            return
        print_table(
            [
                [t.id[:8], t.name, f"v{t.latest_version}", ", ".join(t.tags), t.owner or ""]
                for t in page.items
            ],
            ["ID", "NAME", "LATEST", "TAGS", "OWNER"],
        )

    @prompts.command("show")
    def show(template_id: str, as_json: JsonOpt = False) -> None:
        """Show a template and its version history."""
        ctx = get_context()
        template = call(lambda: PromptService(ctx).get(template_id))
        versions = call(lambda: PromptService(ctx).list_versions(template_id))
        if as_json:
            echo_json(
                {
                    "template": template.model_dump(mode="json"),
                    "versions": [v.model_dump(mode="json") for v in versions],
                }
            )
            return
        typer.echo(f"{template.name}  (v{template.latest_version}, {template.id})")
        if template.description:
            typer.echo(template.description)
        for v in versions:
            lock = "used by a run" if v.locked_at else "unused"
            typer.echo(f"  v{v.version}  {v.created_at:%Y-%m-%d %H:%M}  {lock}  {v.notes or ''}")
        if template.latest:
            typer.echo("\n" + template.latest.body)

    @prompts.command("export")
    def export(
        ids: Annotated[list[str] | None, typer.Argument(help="Template ids (default: all)")] = None,
        output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    ) -> None:
        """Export prompts (every version) as JSON."""
        ctx = get_context()
        bundle = call(lambda: PromptLibraryService(ctx).export(ids or None))
        text = bundle.model_dump_json(indent=2)
        if output:
            output.write_text(text + "\n", encoding="utf-8")
            typer.echo(f"Wrote {len(bundle.templates)} prompt(s) to {output}")
        else:
            typer.echo(text)

    @prompts.command("import")
    def import_(
        path: Path,
        skip_existing: Annotated[
            bool, typer.Option("--skip-existing", help="Skip names that exist instead of renaming")
        ] = False,
    ) -> None:
        """Import an export bundle. Existing prompts are never overwritten."""
        ctx = get_context()
        try:
            bundle = PromptExportBundle.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            typer.secho(f"error: cannot read {path}: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from None
        result = call(
            lambda: PromptLibraryService(ctx).import_bundle(
                bundle, on_conflict="skip" if skip_existing else "rename"
            )
        )
        for t in result.created:
            typer.echo(f"imported  {t.name}  (v{t.latest_version})")
        for s in result.skipped:
            typer.echo(f"skipped   {s['name']}: {s['reason']}")


# --------------------------------------------------------------------------- agents


def _agents(app: typer.Typer) -> None:
    agents = typer.Typer(help="Agent adapters and profiles.", no_args_is_help=True)
    app.add_typer(agents, name="agents")

    @agents.command("discover")
    def discover(as_json: JsonOpt = False) -> None:
        """Report which adapters are installed on this machine (no credentials needed)."""
        ctx = get_context()
        infos = AgentProfileService(ctx).adapters()
        if as_json:
            echo_json([i.model_dump(mode="json") for i in infos])
            return
        print_table(
            [
                [i.name, i.status, i.version or "-", i.executable or "-", i.message or ""]
                for i in infos
            ],
            ["ADAPTER", "STATUS", "VERSION", "EXECUTABLE", "NOTE"],
        )

    @agents.command("list")
    def list_(as_json: JsonOpt = False) -> None:
        """List agent profiles."""
        ctx = get_context()
        page = AgentProfileService(ctx).list_profiles(limit=500)
        if as_json:
            echo_json([p.model_dump(mode="json") for p in page.items])
            return
        print_table(
            [[p.id[:8], p.name, p.adapter_type, f"v{p.latest_version}"] for p in page.items],
            ["ID", "NAME", "ADAPTER", "LATEST"],
        )

    @agents.command("validate")
    def validate(
        adapter_type: str,
        config_file: Annotated[Path, typer.Argument(help="JSON file with the profile config")],
    ) -> None:
        """Validate a profile config without saving it."""
        ctx = get_context()
        try:
            config = json.loads(config_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            typer.secho(f"error: cannot read {config_file}: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from None
        if not isinstance(config, dict):
            raise typer.BadParameter("config must be a JSON object")
        issues = AgentProfileService(ctx).validate_config(adapter_type, config)
        for i in issues:
            color = typer.colors.RED if i.severity == "error" else typer.colors.YELLOW
            typer.secho(f"{i.severity:<8}{i.field}: {i.message}", fg=color)
        if any(i.severity == "error" for i in issues):
            raise typer.Exit(1)
        typer.echo("valid")


# --------------------------------------------------------------------------- projects


def _projects(app: typer.Typer) -> None:
    projects = typer.Typer(help="Projects and path policy.", no_args_is_help=True)
    app.add_typer(projects, name="projects")

    @projects.command("list")
    def list_(as_json: JsonOpt = False) -> None:
        """List projects."""
        ctx = get_context()
        page = ProjectService(ctx).list_projects(limit=500)
        if as_json:
            echo_json([p.model_dump(mode="json") for p in page.items])
            return
        print_table(
            [
                [p.id[:8], p.name, p.repo_path, str((p.git_state or {}).get("branch") or "-")]
                for p in page.items
            ],
            ["ID", "NAME", "PATH", "BRANCH"],
        )

    @projects.command("check-path")
    def check_path(path: str) -> None:
        """Explain whether PATH can become a project (canonical path and allowlist)."""
        ctx = get_context()
        result = ProjectLibraryService(ctx).check_path(path)
        typer.echo(f"canonical: {result.canonical or '-'}")
        typer.echo(f"allowed:   {'yes' if result.allowed else 'no'}")
        typer.echo(result.message)
        if not result.allowed:
            raise typer.Exit(1)


# --------------------------------------------------------------------------- secrets / executables


def _secrets(app: typer.Typer) -> None:
    secrets = typer.Typer(help="Secret references (metadata only).", no_args_is_help=True)
    app.add_typer(secrets, name="secrets")

    @secrets.command("list")
    def list_() -> None:
        """List secret references. Values are never printed."""
        ctx = get_context()
        rows = ProfileLibraryService(ctx).list_secrets()
        print_table(
            [[r.name, r.provider, r.locator, "set" if r.available else "not set"] for r in rows],
            ["NAME", "PROVIDER", "LOCATOR", "ENV"],
        )


def _executables(app: typer.Typer) -> None:
    exes = typer.Typer(help="Executable allowlist for process adapters.", no_args_is_help=True)
    app.add_typer(exes, name="executables")

    @exes.command("list")
    def list_() -> None:
        """Show the effective executable allowlist."""
        ctx = get_context()
        allow = ProfileLibraryService(ctx).executables()
        print_table(
            [[e.path, e.source, "yes" if e.exists else "missing"] for e in allow.entries],
            ["PATH", "SOURCE", "EXISTS"],
        )

    @exes.command("allow")
    def allow(executable: str) -> None:
        """Add an executable (absolute path or name on PATH) to the stored allowlist."""
        ctx = get_context()
        service = ProfileLibraryService(ctx)
        current = service.executables().stored
        result = call(lambda: service.set_executables([*current, executable]))
        typer.echo(f"Allowlisted. {len(result.stored)} stored entr(y/ies).")

    @exes.command("remove")
    def remove(executable: str) -> None:
        """Remove an executable from the stored allowlist."""
        ctx = get_context()
        service = ProfileLibraryService(ctx)
        current = service.executables().stored
        if executable not in current:
            typer.secho(f"error: {executable} is not in the stored allowlist", fg="red", err=True)
            raise typer.Exit(1)
        call(lambda: service.set_executables([e for e in current if e != executable]))
        typer.echo("Removed.")
