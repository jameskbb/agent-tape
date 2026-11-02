"""Demo schedule: a nightly run of the seeded prompt, created **disabled** so nothing runs
until the user turns it on (or presses Run now)."""

from __future__ import annotations

from sqlalchemy import select

from agenttape.application.context import AppContext
from agenttape.application.schemas.schedules import ScheduleCreate
from agenttape.application.services.schedules import ScheduleService
from agenttape.infrastructure.db.models import Schedule
from agenttape.seeds.base import SeedReport
from agenttape.seeds.core_demo import IMPROVE_PROMPT

ORDER = 30

NIGHTLY_SCHEDULE = "Nightly greeting service check"


def seed(ctx: AppContext, report: SeedReport) -> None:
    with ctx.session() as session:
        exists = session.scalars(
            select(Schedule.id).where(Schedule.name == NIGHTLY_SCHEDULE)
        ).first()
    if exists:
        report.add("schedule", NIGHTLY_SCHEDULE, created=False)
        report.data.setdefault("schedules", {})[NIGHTLY_SCHEDULE] = exists
        return
    profile_id = (report.data.get("profiles") or {}).get("success")
    prompt_id = (report.data.get("prompts") or {}).get(IMPROVE_PROMPT)
    if not profile_id or not prompt_id:
        return
    created = ScheduleService(ctx).create(
        ScheduleCreate(
            name=NIGHTLY_SCHEDULE,
            description=(
                "Every night at 02:00 New York time, ask the Demo Agent to improve the greeting "
                "service. Disabled by default: enable it, or press Run now to try it."
            ),
            kind="cron",
            cron_expression="0 2 * * *",
            timezone="America/New_York",
            target_type="prompt",
            project_id=report.data.get("project_id"),
            prompt_template_id=prompt_id,
            agent_profile_id=profile_id,
            inputs={"goal": "Validate the name argument and reject empty names."},
            enabled=False,
            misfire_policy="run_once",
            concurrency_policy="skip",
            max_queued=1,
        ),
        actor="demo seed",
        is_demo=True,
    )
    report.add("schedule", NIGHTLY_SCHEDULE, created=True)
    report.data.setdefault("schedules", {})[NIGHTLY_SCHEDULE] = created.id
