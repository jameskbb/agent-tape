"""LimitsService: global limits (Settings > Limits) and a run's effective limits."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from agenttape.application.context import AppContext
from agenttape.application.schemas.schedules import (
    BudgetCheckOut,
    LimitsOut,
    LimitsSettings,
    RunLimitsOut,
)
from agenttape.application.services.audit import record_audit
from agenttape.application.services.common import get_or_404
from agenttape.domain.enums import QueueItemStatus
from agenttape.domain.errors import ValidationError
from agenttape.domain.retry import RetryPolicy
from agenttape.infrastructure.db.models import QueueItem, Run
from agenttape.limits.budget import (
    BUDGET_KEYS,
    Measures,
    budget_from_snapshot,
    evaluate,
    merge_budgets,
)
from agenttape.limits.concurrency import running_in_scope, scopes_for
from agenttape.limits.settings import load_limits, save_limits


class LimitsService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    def get(self) -> LimitsOut:
        with self.ctx.session() as session:
            limits = load_limits(session)
            rows = session.execute(
                select(Run.adapter_type, Run.project_id, func.count())
                .select_from(QueueItem)
                .join(Run, QueueItem.run_id == Run.id)
                .where(QueueItem.status == QueueItemStatus.LEASED.value)
                .group_by(Run.adapter_type, Run.project_id)
            ).all()
        by_adapter: dict[str, int] = {}
        by_project: dict[str, int] = {}
        total = 0
        for adapter, project, count in rows:
            total += int(count)
            if adapter:
                by_adapter[adapter] = by_adapter.get(adapter, 0) + int(count)
            if project:
                by_project[project] = by_project.get(project, 0) + int(count)
        s = self.ctx.settings
        return LimitsOut(
            settings=limits,
            budget_keys=[
                {"key": k, "label": label, "unit": unit, "description": desc}
                for k, (label, unit, desc) in BUDGET_KEYS.items()
            ],
            active_runs=total,
            active_by_adapter=by_adapter,
            active_by_project=by_project,
            worker_defaults={
                "worker_concurrency": s.worker_concurrency,
                "default_timeout_seconds": s.default_timeout_seconds,
                "default_idle_timeout_seconds": s.default_idle_timeout_seconds,
                "default_max_attempts": s.default_max_attempts,
                "retry_backoff_base_seconds": s.retry_backoff_base_seconds,
                "retry_backoff_max_seconds": s.retry_backoff_max_seconds,
            },
        )

    def update(self, data: LimitsSettings, *, actor: str = "local user") -> LimitsOut:
        for adapter, value in data.per_adapter.items():
            if value < 1:
                raise ValidationError(f"Adapter limit for '{adapter}' must be at least 1")
        cleaned = merge_budgets([("settings", data.default_budget)])
        bad = sorted(set(data.default_budget) - {*BUDGET_KEYS, "warn_ratio"})
        if bad:
            raise ValidationError(
                f"Unknown budget keys: {', '.join(bad)}",
                details={"allowed": [*BUDGET_KEYS, "warn_ratio"]},
            )
        data = data.model_copy(
            update={"default_budget": cleaned.as_dict() if cleaned.limits else {}}
        )
        with self.ctx.session() as session:
            before = load_limits(session)
            save_limits(session, self.ctx.clock, data)
            record_audit(
                session,
                self.ctx.clock,
                action="limits.update",
                resource_type="settings",
                resource_id=None,
                actor=actor,
                details={
                    "before": before.model_dump(mode="json"),
                    "after": data.model_dump(mode="json"),
                },
            )
        return self.get()

    def for_run(self, run_id: str) -> RunLimitsOut:
        with self.ctx.session() as session:
            run = get_or_404(session, Run, run_id, "run")
            default = load_limits(session).default_budget
            budget = budget_from_snapshot(dict(run.snapshot or {}), run.budget, default)
            from agenttape.infrastructure.db.models import Artifact, FileChange

            files, ins, dels = session.execute(
                select(
                    func.count(FileChange.id),
                    func.coalesce(func.sum(FileChange.insertions), 0),
                    func.coalesce(func.sum(FileChange.deletions), 0),
                ).where(FileChange.run_id == run_id)
            ).one()
            artifact_bytes = session.scalar(
                select(func.coalesce(func.sum(Artifact.size_bytes), 0)).where(
                    Artifact.run_id == run_id
                )
            )
            checks = evaluate(
                budget,
                Measures(
                    usage=run.usage,
                    cost=run.cost,
                    changed_files=int(files or 0),
                    diff_lines=int(ins or 0) + int(dels or 0),
                    artifact_bytes=int(artifact_bytes or 0),
                ),
            )
            options = dict(run.options or {})
            policy = RetryPolicy.from_config(
                options.get("retry"),
                default_max_attempts=run.max_attempts,
                default_base=self.ctx.settings.retry_backoff_base_seconds,
                default_max=self.ctx.settings.retry_backoff_max_seconds,
            )
            retry: dict[str, Any] = {
                "retry_on": sorted(c.value for c in policy.retry_on),
                "backoff_base_seconds": policy.backoff_base_seconds,
                "backoff_max_seconds": policy.backoff_max_seconds,
                "jitter_ratio": policy.jitter_ratio,
                "configured": dict(options.get("retry") or {}),
                "delays_seconds": [
                    policy.backoff_seconds(n) for n in range(1, max(1, run.max_attempts))
                ],
            }
            concurrency = [
                {
                    "scope": s.kind,
                    "label": s.label,
                    "limit": s.limit,
                    "running": running_in_scope(session, s),
                }
                for s in scopes_for(session, run)
            ]
            return RunLimitsOut(
                run_id=run.id,
                budget=budget.as_dict() if budget.limits else {},
                budget_sources=budget.sources,
                checks=[BudgetCheckOut(**c.as_dict()) for c in checks],
                timeout_seconds=options.get("timeout_seconds"),
                idle_timeout_seconds=options.get("idle_timeout_seconds"),
                max_attempts=run.max_attempts,
                attempt_count=run.attempt_count,
                retry=retry,
                concurrency=concurrency,
            )
