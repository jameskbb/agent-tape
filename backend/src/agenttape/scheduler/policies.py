"""Misfire and concurrency decisions for due occurrences (pure; no I/O).

Misfire policy decides what happens to occurrences whose due time passed while no
scheduler was running (or the machine slept):

* ``skip``     - occurrences older than the grace period are recorded as ``missed``; only
                 on-time occurrences fire.
* ``run_once`` - if any occurrences were missed, exactly one run fires now (for the most
                 recent missed time); the older ones are recorded as ``missed``.

Concurrency policy decides what happens when an occurrence fires while an earlier run of
the same schedule is still active (queued, running, or waiting for approval):

* ``queue``              - the occurrence waits (status ``queued``) and starts when the
                           active run finishes; at most ``max_queued`` wait, extra ones are
                           ``skipped``.
* ``skip``               - the occurrence is recorded as ``skipped``.
* ``cancel_and_replace`` - the active run is cancelled and the new one starts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum


class MisfirePolicy(StrEnum):
    SKIP = "skip"
    RUN_ONCE = "run_once"


class ConcurrencyPolicy(StrEnum):
    QUEUE = "queue"
    SKIP = "skip"
    CANCEL_AND_REPLACE = "cancel_and_replace"


class OccurrenceStatus(StrEnum):
    #: Inserted, run not created yet (a crash here is finished on the next tick).
    PENDING = "pending"
    #: Waiting for the schedule's active run to finish (concurrency policy ``queue``).
    QUEUED = "queued"
    CREATED = "created"
    SKIPPED = "skipped"
    MISSED = "missed"
    #: A later occurrence cancelled this occurrence's run (``cancel_and_replace``).
    REPLACED = "replaced"
    FAILED = "failed"


class ConcurrencyAction(StrEnum):
    START = "start"
    WAIT = "wait"
    SKIP = "skip"
    REPLACE = "replace"


#: Occurrences due longer ago than this are "missed" (misfired), not on time.
DEFAULT_MISFIRE_GRACE_SECONDS = 60
#: Most individual ``missed`` rows recorded per tick (the audit entry has the full count).
MAX_MISSED_ROWS = 50


@dataclass
class DuePlan:
    fire: list[datetime] = field(default_factory=list)
    missed: list[datetime] = field(default_factory=list)
    missed_total: int = 0


def plan_due(
    due: list[datetime],
    now: datetime,
    policy: MisfirePolicy | str,
    grace_seconds: float = DEFAULT_MISFIRE_GRACE_SECONDS,
) -> DuePlan:
    """Split ``due`` (ascending, all <= now) into occurrences to fire and ones missed."""
    grace = timedelta(seconds=grace_seconds)
    on_time = [d for d in due if now - d <= grace]
    late = [d for d in due if now - d > grace]
    plan = DuePlan()
    if MisfirePolicy(policy) == MisfirePolicy.SKIP:
        plan.fire = on_time
        plan.missed = late
    else:
        if on_time:
            plan.fire = on_time
            plan.missed = late
        elif late:
            plan.fire = [late[-1]]
            plan.missed = late[:-1]
    plan.missed_total = len(plan.missed)
    plan.missed = plan.missed[-MAX_MISSED_ROWS:]
    return plan


def decide_concurrency(
    policy: ConcurrencyPolicy | str,
    *,
    active_runs: int,
    queued_occurrences: int,
    max_queued: int,
) -> ConcurrencyAction:
    """What to do with an occurrence that is ready to start."""
    if active_runs <= 0 and queued_occurrences <= 0:
        return ConcurrencyAction.START
    p = ConcurrencyPolicy(policy)
    if p == ConcurrencyPolicy.SKIP:
        return ConcurrencyAction.SKIP if active_runs > 0 else ConcurrencyAction.START
    if p == ConcurrencyPolicy.CANCEL_AND_REPLACE:
        return ConcurrencyAction.REPLACE
    # queue: wait behind the active run (and any earlier queued occurrences), bounded.
    if queued_occurrences >= max(0, max_queued):
        return ConcurrencyAction.SKIP
    return ConcurrencyAction.WAIT
