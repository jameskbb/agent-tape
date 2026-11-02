"""Demo data boundary: seed, reset (demo rows only), and start live demo runs.

``reset`` deletes rows whose ``is_demo`` column is true, table by table in reverse
foreign-key order, for *every* mapped table that has an ``is_demo`` column - including
tables added by extensions - and relies on ``ON DELETE`` rules for children. It never
issues an unfiltered delete, so non-demo data cannot be touched.
"""

from __future__ import annotations

import shutil
from typing import Any

from sqlalchemy import Table, delete, select

from agenttape.application.context import AppContext
from agenttape.application.schemas.runs import RunCreate, RunCreateResult
from agenttape.application.services.audit import record_audit
from agenttape.application.services.runs import RunService
from agenttape.domain.enums import RunSource
from agenttape.domain.errors import ValidationError
from agenttape.infrastructure.db.base import Base
from agenttape.infrastructure.db.models import Run
from agenttape.seeds.base import SeedReport, load_seeds
from agenttape.seeds.core_demo import DEMO_PROFILES, IMPROVE_PROMPT


def demo_tables() -> list[Table]:
    """Tables with an ``is_demo`` column, children before parents."""
    return [t for t in reversed(Base.metadata.sorted_tables) if "is_demo" in t.c]


class DemoService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    def seed(self, *, include_runs: bool = True, execute_inline: bool = True) -> SeedReport:
        """Run every seed module. ``execute_inline=False`` only queues demo runs."""
        report = SeedReport(include_runs=include_runs, execute_inline=execute_inline)
        for _order, _name, fn in load_seeds():
            fn(self.ctx, report)
        return report

    def reset(self, *, actor: str = "local user") -> dict[str, int]:
        with self.ctx.session() as session:
            run_ids = list(session.scalars(select(Run.id).where(Run.is_demo.is_(True))))
        deleted: dict[str, int] = {}
        with self.ctx.session() as session:
            for table in demo_tables():
                result = session.execute(delete(table).where(table.c.is_demo.is_(True)))
                count = int(result.rowcount or 0)  # type: ignore[attr-defined]
                if count:
                    deleted[table.name] = count
            record_audit(
                session,
                self.ctx.clock,
                action="demo.reset",
                resource_type="demo",
                resource_id=None,
                actor=actor,
                details={"deleted": deleted},
            )
        settings = self.ctx.settings
        for run_id in run_ids:
            for path in (
                settings.artifacts_dir / "runs" / run_id,
                settings.logs_dir / run_id,
                settings.workspaces_dir / "demo" / run_id,
            ):
                shutil.rmtree(path, ignore_errors=True)
        return deleted

    def start_run(
        self,
        *,
        scenario: str = "success",
        source: RunSource = RunSource.UI,
        inputs: dict[str, Any] | None = None,
        actor: str = "local user",
    ) -> RunCreateResult:
        if scenario not in DEMO_PROFILES:
            raise ValidationError(
                f"Unknown demo scenario '{scenario}'", details={"scenarios": list(DEMO_PROFILES)}
            )
        report = self.seed(include_runs=False)
        profile_id = report.data["profiles"][scenario]
        prompt_id = report.data["prompts"][IMPROVE_PROMPT]
        return RunService(self.ctx).create_run(
            RunCreate(
                agent_profile_id=profile_id,
                prompt_template_id=prompt_id,
                project_id=report.data["project_id"],
                inputs=inputs or {},
                source=source,
                is_demo=True,
            ),
            actor=actor,
        )
