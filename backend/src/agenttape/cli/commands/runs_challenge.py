"""``runs challenge`` and ``runs challenges``: on-demand review of a finished run's
findings, through the same ``FindingReviewService`` the API calls.
The one-engine rule (CONTRIBUTING.md) means neither command may duplicate the service's
own logic; both only parse the request and call ``FindingReviewService(ctx)``.

Added to the existing ``runs`` command group via ``_runs_group`` (``runs_lineage.py``,
which registers before this module by import-sort order).
"""

from __future__ import annotations

from typing import Annotated, Any

import typer

from agenttape.application.schemas.finding_review import ChallengeCreate, ChallengeOut
from agenttape.application.services.finding_review import FindingReviewService
from agenttape.cli.commands.runs import _resolve, _wait
from agenttape.cli.commands.runs_lineage import _runs_group, full_run_id
from agenttape.cli.common import call, echo_json, get_context, status_text
from agenttape.infrastructure.db.models import AgentProfile


def _usage_str(usage: dict[str, Any] | None) -> str:
    if not usage:
        return "unavailable"
    input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
    if input_tokens is None and output_tokens is None:
        return "unavailable"
    label = " (simulated)" if usage.get("simulated") else ""
    return (
        f"{input_tokens if input_tokens is not None else '?'} in / "
        f"{output_tokens if output_tokens is not None else '?'} out{label}"
    )


def _cost_str(cost: dict[str, Any] | None) -> str:
    """Never a computed or zero stand-in: only what the run actually recorded."""
    if not cost:
        return "unavailable"
    amount = cost.get("total_usd", cost.get("amount"))
    return "unavailable" if amount is None else f"${amount:.4f}"


def _print_challenge(challenge: ChallengeOut) -> None:
    run = challenge.run
    typer.echo(
        f"{run.id} [{status_text(challenge.state)}] trigger={challenge.trigger}  "
        f"usage {_usage_str(run.usage)}  cost {_cost_str(run.cost)}"
    )
    for outcome in challenge.outcomes:
        typer.echo(f"  {outcome.target_finding_key}: {outcome.outcome} (model judgment)")
    if challenge.unresolved:
        typer.echo(f"  {len(challenge.unresolved)} unresolved review finding(s)")


def register(app: typer.Typer) -> None:
    runs_app = _runs_group(app)

    @runs_app.command("challenge")
    def challenge(
        run_id: str,
        profile: Annotated[
            str, typer.Option("--profile", help="Reviewer agent profile id or name")
        ],
        profile_version: Annotated[
            str | None, typer.Option("--profile-version", help="Agent profile version id")
        ] = None,
        title: Annotated[str | None, typer.Option(help="Title for the challenge run")] = None,
        follow: Annotated[
            bool, typer.Option("--follow", "-f", help="Stream events until the challenge settles")
        ] = False,
        inline: Annotated[
            bool | None, typer.Option("--inline/--no-inline", help="Execute in this process")
        ] = None,
        as_json: Annotated[bool, typer.Option("--json")] = False,
    ) -> None:
        """Challenge a finished run's findings with a chosen reviewer agent profile."""
        ctx = get_context()
        target_id = full_run_id(ctx, run_id)
        result = call(
            lambda: FindingReviewService(ctx).start_challenge(
                target_id,
                ChallengeCreate(
                    # An id or a name, resolved exactly as `runs start` resolves it.
                    agent_profile_id=_resolve(ctx, AgentProfile, profile, "agent profile"),
                    agent_profile_version_id=profile_version,
                    title=title,
                ),
                actor="cli",
            )
        )
        if as_json:
            echo_json(result.model_dump(mode="json"))
        else:
            run = result.run
            typer.echo(f"Challenge run {run.id} [{status_text(run.status)}] {run.title}")
            typer.echo(
                f"  reviewing {len(result.findings_reviewed)} finding(s) of run {target_id} "
                "(model judgment, never counts as proof)"
            )
            if result.omitted:
                typer.echo(
                    f"  {result.omitted} finding(s) left out: over the reviewer prompt budget"
                )
            if result.neutralized:
                typer.echo(
                    f"  {len(result.neutralized)} field(s) had delimiter-shaped text replaced"
                )
        if follow:
            _wait(ctx, result.run.id, follow=True, inline=inline)

    @runs_app.command("challenges")
    def challenges(
        run_id: str,
        as_json: Annotated[bool, typer.Option("--json")] = False,
    ) -> None:
        """List a run's challenges, each labelled model judgment, never proof."""
        ctx = get_context()
        target_id = full_run_id(ctx, run_id)
        results = call(lambda: FindingReviewService(ctx).challenges(target_id))
        if as_json:
            echo_json([c.model_dump(mode="json") for c in results])
            return
        if not results:
            typer.echo(f"No challenges for run {target_id}")
            return
        for challenge_out in results:
            _print_challenge(challenge_out)
