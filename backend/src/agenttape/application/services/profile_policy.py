"""Local-policy validation for agent profiles.

Called from :meth:`AgentProfileService.validate_config`. Checks the engine-level keys every
adapter shares (budget, allowed/denied tools, permissions, retry) and, for adapters that
implement ``validate_with_policy``, the executable allowlist and secret references.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from agenttape.adapters.base import AgentAdapter
from agenttape.adapters.policy import PolicyAwareAdapter, ProfilePolicy
from agenttape.application.context import AppContext
from agenttape.application.schemas.catalog import ValidationIssueOut
from agenttape.application.services.workspaces import allowed_roots, default_workspace_id
from agenttape.infrastructure.db.models import SecretReference

BUDGET_KEYS = {
    "max_cost_usd",
    "max_input_tokens",
    "max_output_tokens",
    "max_total_tokens",
    "max_changed_files",
    "max_diff_lines",
    "max_artifact_bytes",
}


def build_policy(ctx: AppContext) -> ProfilePolicy:
    with ctx.session() as session:
        ws_id = default_workspace_id(session, ctx.clock)
        names = frozenset(
            session.scalars(
                select(SecretReference.name).where(SecretReference.workspace_id == ws_id)
            ).all()
        )
        roots = allowed_roots(session, ctx.settings)
    return ProfilePolicy(data_dir=ctx.settings.data_dir, allowed_roots=roots, secret_names=names)


def common_issues(config: dict[str, Any]) -> list[ValidationIssueOut]:
    issues: list[ValidationIssueOut] = []
    budget = config.get("budget")
    if budget is not None:
        if not isinstance(budget, dict):
            issues.append(ValidationIssueOut(field="budget", message="must be an object"))
        else:
            for key, value in budget.items():
                if key not in BUDGET_KEYS:
                    issues.append(
                        ValidationIssueOut(
                            field=f"budget.{key}",
                            message="is not a known budget limit",
                            severity="warning",
                        )
                    )
                elif not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
                    issues.append(
                        ValidationIssueOut(field=f"budget.{key}", message="must be positive")
                    )
    for key in ("allowed_tools", "denied_tools"):
        value = config.get(key)
        if value is not None and (
            not isinstance(value, list) or not all(isinstance(v, str) and v for v in value)
        ):
            issues.append(ValidationIssueOut(field=key, message="must be a list of tool names"))
    permissions = config.get("permissions")
    if permissions is not None and not isinstance(permissions, str | dict):
        issues.append(
            ValidationIssueOut(field="permissions", message="must be a mode name or an object")
        )
    retry = config.get("retry")
    if retry is not None and not isinstance(retry, dict):
        issues.append(ValidationIssueOut(field="retry", message="must be an object"))
    return issues


def policy_issues(
    ctx: AppContext, adapter: AgentAdapter, config: dict[str, Any]
) -> list[ValidationIssueOut]:
    issues = common_issues(config)
    if isinstance(adapter, PolicyAwareAdapter):
        policy = build_policy(ctx)
        issues.extend(
            ValidationIssueOut(field=i.field, message=i.message, severity=i.severity)
            for i in adapter.validate_with_policy(config, policy)
        )
    return issues
