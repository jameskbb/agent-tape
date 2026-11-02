"""Overview dashboard aggregates.

Every number here is computed from recorded runs; nothing is estimated. Where a value
cannot be known (no finished runs, no adapter reported usage or cost) the field is
``None`` and the UI shows an honest unavailable state. Demo usage is marked simulated
and is counted separately from real usage, never mixed into it. The rules for "real"
usage and cost are shared with budgets (``limits.budget.reported_tokens`` /
``reported_cost_usd``).
"""

from __future__ import annotations

import statistics
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select

from agenttape.application.context import AppContext
from agenttape.application.schemas.catalog import ApprovalOut
from agenttape.application.schemas.findings import FindingsTallyOut
from agenttape.application.schemas.runs import RunOut
from agenttape.application.services.runs import RunService
from agenttape.domain.enums import ApprovalStatus, RunStatus
from agenttape.infrastructure.clock import Clock
from agenttape.infrastructure.db.models import ApprovalRequest, Run, Schedule
from agenttape.limits.budget import reported_cost_usd, reported_tokens

#: Statuses that count towards the success rate (cancelled and skipped are excluded:
#: they say nothing about whether the agent could do the work).
OUTCOME_STATUSES = (RunStatus.SUCCEEDED.value, RunStatus.FAILED.value, RunStatus.TIMED_OUT.value)
LIVE_STATUSES = (
    RunStatus.QUEUED.value,
    RunStatus.PREPARING.value,
    RunStatus.RUNNING.value,
    RunStatus.CANCELLING.value,
    RunStatus.WAITING_FOR_APPROVAL.value,
)
MAX_WINDOW_DAYS = 90
MAX_RUNS_SCANNED = 50_000


def window_start(clock: Clock, days: int) -> datetime:
    """The start (UTC start of day) of a ``days``-day window ending today, with ``days``
    clamped to ``1..MAX_WINDOW_DAYS``: the one day boundary the dashboard and every findings
    surface share."""
    days = max(1, min(days, MAX_WINDOW_DAYS))
    start_day = clock.now().astimezone(UTC).date() - timedelta(days=days - 1)
    return datetime(start_day.year, start_day.month, start_day.day, tzinfo=UTC)


class DayActivity(BaseModel):
    date: str
    succeeded: int
    failed: int
    other: int


class CategoryCount(BaseModel):
    category: str
    count: int


class UpcomingSchedule(BaseModel):
    id: str
    name: str
    kind: str
    timezone: str
    next_run_at: datetime


class SuccessRate(BaseModel):
    succeeded: int
    finished: int
    rate: float | None


class DurationStats(BaseModel):
    median_ms: int | None
    p90_ms: int | None
    sample: int


class UsageSummary(BaseModel):
    #: Runs whose adapter reported real (non-simulated) token counts in the window.
    runs_reporting: int
    total_tokens: int | None
    #: Runs whose usage was simulated (Demo Agent); never included in ``total_tokens``.
    simulated_runs: int


class CostSummary(BaseModel):
    runs_reporting: int
    total_usd: float | None


class Dashboard(BaseModel):
    generated_at: datetime
    window_days: int
    live: dict[str, int]
    live_runs: list[RunOut]
    pending_approvals: list[ApprovalOut]
    pending_approval_count: int
    upcoming_schedules: list[UpcomingSchedule]
    enabled_schedule_count: int
    recent_failures: list[RunOut]
    runs_in_window: int
    success_rate: SuccessRate
    duration: DurationStats
    activity: list[DayActivity]
    failure_categories: list[CategoryCount]
    usage: UsageSummary
    cost: CostSummary
    #: The same cross-run read model as ``GET /findings``: one
    #: tally, one verdict vocabulary, no second query.
    findings: FindingsTallyOut


def _percentile(values: list[int], pct: float) -> int:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(pct * (len(ordered) - 1))))
    return ordered[index]


class DashboardService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    def summary(self, *, days: int = 14) -> Dashboard:
        # Local import: avoids a module-level cycle (findings.py imports MAX_WINDOW_DAYS and
        # window_start from this module).
        from agenttape.application.services.findings import FindingsService

        days = max(1, min(days, MAX_WINDOW_DAYS))
        findings_tally = FindingsService(self.ctx).window_tally(days)
        now = self.ctx.clock.now()
        start = window_start(self.ctx.clock, days)
        start_day = start.date()
        runs = RunService(self.ctx)
        with self.ctx.session() as session:
            live_counts = {
                str(status): int(count)
                for status, count in session.execute(
                    select(Run.status, func.count())
                    .where(Run.status.in_(LIVE_STATUSES))
                    .group_by(Run.status)
                ).all()
            }
            live_rows = session.scalars(
                select(Run)
                .where(Run.status.in_(LIVE_STATUSES))
                .order_by(Run.priority.desc(), Run.created_at.asc())
                .limit(8)
            ).all()
            live_runs = [runs._run_out(session, r) for r in live_rows]
            approvals_q = select(ApprovalRequest).where(
                ApprovalRequest.status == ApprovalStatus.PENDING.value
            )
            pending_count = int(
                session.scalar(select(func.count()).select_from(approvals_q.subquery())) or 0
            )
            pending = [
                ApprovalOut.model_validate(a)
                for a in session.scalars(
                    approvals_q.order_by(ApprovalRequest.created_at.asc()).limit(5)
                )
            ]
            upcoming = [
                UpcomingSchedule(
                    id=s.id,
                    name=s.name,
                    kind=s.kind,
                    timezone=s.timezone,
                    next_run_at=s.next_run_at,
                )
                for s in session.scalars(
                    select(Schedule)
                    .where(Schedule.enabled.is_(True), Schedule.next_run_at.is_not(None))
                    .order_by(Schedule.next_run_at.asc())
                    .limit(5)
                )
                if s.next_run_at is not None
            ]
            enabled_count = int(
                session.scalar(
                    select(func.count()).select_from(Schedule).where(Schedule.enabled.is_(True))
                )
                or 0
            )
            failures = [
                runs._run_out(session, r)
                for r in session.scalars(
                    select(Run)
                    .where(Run.status.in_((RunStatus.FAILED.value, RunStatus.TIMED_OUT.value)))
                    .order_by(
                        Run.finished_at.is_(None), Run.finished_at.desc(), Run.created_at.desc()
                    )
                    .limit(5)
                )
            ]
            rows = session.execute(
                select(
                    Run.created_at,
                    Run.status,
                    Run.duration_ms,
                    Run.failure_category,
                    Run.usage,
                    Run.cost,
                )
                .where(Run.created_at >= start)
                .limit(MAX_RUNS_SCANNED)
            ).all()

        buckets: dict[str, dict[str, int]] = {
            (start_day + timedelta(days=i)).isoformat(): {"succeeded": 0, "failed": 0, "other": 0}
            for i in range(days)
        }
        categories: Counter[str] = Counter()
        durations: list[int] = []
        succeeded = finished = 0
        token_total = 0.0
        token_runs = simulated_runs = 0
        cost_total = 0.0
        cost_runs = 0
        for created_at, status, duration_ms, category, usage, cost in rows:
            day = created_at.astimezone(UTC).date().isoformat()
            bucket = buckets.get(day)
            if status in LIVE_STATUSES:
                pass
            elif bucket is not None:
                if status == RunStatus.SUCCEEDED.value:
                    bucket["succeeded"] += 1
                elif status in (RunStatus.FAILED.value, RunStatus.TIMED_OUT.value):
                    bucket["failed"] += 1
                else:
                    bucket["other"] += 1
            if status in OUTCOME_STATUSES:
                finished += 1
                succeeded += status == RunStatus.SUCCEEDED.value
                if duration_ms is not None:
                    durations.append(int(duration_ms))
            if status in (RunStatus.FAILED.value, RunStatus.TIMED_OUT.value) and category:
                categories[str(category)] += 1
            usage_d: dict[str, Any] | None = usage
            if usage_d and usage_d.get("simulated"):
                simulated_runs += 1
            else:
                tokens, _reason = reported_tokens(usage_d)
                if tokens is not None:
                    token_runs += 1
                    token_total += tokens
            usd, _why = reported_cost_usd(cost)
            if usd is not None:
                cost_runs += 1
                cost_total += usd

        return Dashboard(
            generated_at=now,
            window_days=days,
            live={s: live_counts.get(s, 0) for s in LIVE_STATUSES},
            live_runs=live_runs,
            pending_approvals=pending,
            pending_approval_count=pending_count,
            upcoming_schedules=upcoming,
            enabled_schedule_count=enabled_count,
            recent_failures=failures,
            runs_in_window=len(rows),
            success_rate=SuccessRate(
                succeeded=succeeded,
                finished=finished,
                rate=round(succeeded / finished, 4) if finished else None,
            ),
            duration=DurationStats(
                median_ms=int(statistics.median(durations)) if durations else None,
                p90_ms=_percentile(durations, 0.9) if durations else None,
                sample=len(durations),
            ),
            activity=[DayActivity(date=d, **v) for d, v in buckets.items()],
            failure_categories=[
                CategoryCount(category=c, count=n) for c, n in categories.most_common(6)
            ],
            usage=UsageSummary(
                runs_reporting=token_runs,
                total_tokens=int(token_total) if token_runs else None,
                simulated_runs=simulated_runs,
            ),
            cost=CostSummary(
                runs_reporting=cost_runs, total_usd=round(cost_total, 6) if cost_runs else None
            ),
            findings=findings_tally,
        )
