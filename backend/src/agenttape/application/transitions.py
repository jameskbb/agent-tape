"""Race-safe status changes for runs, attempts, and steps.

Each helper validates the move against the domain transition table and then performs a
conditional ``UPDATE ... WHERE id = :id AND status = :expected``. If another writer changed
the row first, ``rowcount`` is 0 and :class:`ConcurrentUpdateError` is raised instead of
silently overwriting (e.g. a cancel racing a successful finish).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from agenttape.domain import states
from agenttape.domain.enums import RunStatus
from agenttape.domain.errors import ConflictError
from agenttape.infrastructure.db.models import Run, RunAttempt, StepRun
from agenttape.security.persistence import GUARDED, redact_column


class ConcurrentUpdateError(ConflictError):
    code = "concurrent_update"


def _apply(
    session: Session,
    model: type[Run] | type[RunAttempt] | type[StepRun],
    obj: Run | RunAttempt | StepRun,
    kind: str,
    target: RunStatus,
    fields: dict[str, Any],
) -> None:
    expected = obj.status
    guarded = GUARDED.get(model, ())
    fields = {k: (redact_column(v) if k in guarded else v) for k, v in fields.items()}
    result = session.execute(
        update(model)
        .where(model.id == obj.id, model.status == expected)
        .values(status=target.value, **fields)
        .execution_options(synchronize_session=False)
    )
    if (result.rowcount or 0) != 1:  # type: ignore[attr-defined]
        session.expire(obj)
        raise ConcurrentUpdateError(
            f"{kind} {obj.id} changed concurrently (expected status '{expected}')",
            details={"id": obj.id, "expected": expected, "target": target.value},
        )
    session.expire(obj)


def transition_run(session: Session, run: Run, target: RunStatus, **fields: Any) -> None:
    states.check_run(run.status, target)
    _apply(session, Run, run, "run", target, fields)


def transition_attempt(
    session: Session, attempt: RunAttempt, target: RunStatus, **fields: Any
) -> None:
    states.check_attempt(attempt.status, target)
    _apply(session, RunAttempt, attempt, "attempt", target, fields)


def transition_step(session: Session, step: StepRun, target: RunStatus, **fields: Any) -> None:
    states.check_step(step.status, target)
    _apply(session, StepRun, step, "step", target, fields)
