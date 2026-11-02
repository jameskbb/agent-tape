"""Explicit state machines for runs, attempts, and steps.

Every status change in the system goes through :func:`check_transition` (services
additionally perform a conditional ``UPDATE ... WHERE status = <expected>`` so two
writers racing on the same record cannot both win).
"""

from __future__ import annotations

from collections.abc import Mapping

from agenttape.domain.enums import RunStatus
from agenttape.domain.errors import InvalidTransitionError

S = RunStatus

#: Run transitions. ``failed``/``timed_out``/``cancelled`` may return to ``queued`` only via
#: an explicit retry-from-failure (a new attempt on the same durable run record).
RUN_TRANSITIONS: Mapping[RunStatus, frozenset[RunStatus]] = {
    S.QUEUED: frozenset({S.PREPARING, S.WAITING_FOR_APPROVAL, S.CANCELLED, S.SKIPPED, S.FAILED}),
    S.PREPARING: frozenset(
        {
            S.RUNNING,
            S.WAITING_FOR_APPROVAL,
            S.CANCELLING,
            S.CANCELLED,
            S.FAILED,
            S.TIMED_OUT,
            S.QUEUED,
        }
    ),
    S.RUNNING: frozenset(
        {
            S.SUCCEEDED,
            S.FAILED,
            S.TIMED_OUT,
            S.CANCELLING,
            S.CANCELLED,
            S.WAITING_FOR_APPROVAL,
            S.QUEUED,
        }
    ),
    S.WAITING_FOR_APPROVAL: frozenset({S.QUEUED, S.FAILED, S.CANCELLED}),
    S.CANCELLING: frozenset({S.CANCELLED, S.FAILED, S.TIMED_OUT, S.SUCCEEDED}),
    S.FAILED: frozenset({S.QUEUED}),
    S.TIMED_OUT: frozenset({S.QUEUED}),
    S.CANCELLED: frozenset({S.QUEUED}),
    S.SUCCEEDED: frozenset(),
    S.SKIPPED: frozenset(),
}

#: Attempt transitions. An attempt is one execution; it never returns to a queue.
#: ``waiting_for_approval -> running`` is the resume path after an approval.
ATTEMPT_TRANSITIONS: Mapping[RunStatus, frozenset[RunStatus]] = {
    S.PREPARING: frozenset(
        {S.RUNNING, S.WAITING_FOR_APPROVAL, S.CANCELLED, S.FAILED, S.TIMED_OUT, S.CANCELLING}
    ),
    S.RUNNING: frozenset(
        {S.SUCCEEDED, S.FAILED, S.TIMED_OUT, S.CANCELLING, S.CANCELLED, S.WAITING_FOR_APPROVAL}
    ),
    S.WAITING_FOR_APPROVAL: frozenset({S.RUNNING, S.FAILED, S.CANCELLED}),
    S.CANCELLING: frozenset({S.CANCELLED, S.FAILED, S.TIMED_OUT, S.SUCCEEDED}),
    S.QUEUED: frozenset(),
    S.SUCCEEDED: frozenset(),
    S.FAILED: frozenset(),
    S.TIMED_OUT: frozenset(),
    S.CANCELLED: frozenset(),
    S.SKIPPED: frozenset(),
}

#: Workflow step transitions (used by the workflow engine for each node).
STEP_TRANSITIONS: Mapping[RunStatus, frozenset[RunStatus]] = {
    S.QUEUED: frozenset(
        {S.PREPARING, S.RUNNING, S.WAITING_FOR_APPROVAL, S.SKIPPED, S.CANCELLED, S.FAILED}
    ),
    S.PREPARING: frozenset(
        {S.RUNNING, S.WAITING_FOR_APPROVAL, S.CANCELLED, S.FAILED, S.TIMED_OUT, S.SKIPPED}
    ),
    S.RUNNING: frozenset(
        {S.SUCCEEDED, S.FAILED, S.TIMED_OUT, S.CANCELLING, S.CANCELLED, S.WAITING_FOR_APPROVAL}
    ),
    S.WAITING_FOR_APPROVAL: frozenset({S.RUNNING, S.SUCCEEDED, S.FAILED, S.CANCELLED, S.QUEUED}),
    S.CANCELLING: frozenset({S.CANCELLED, S.FAILED, S.SUCCEEDED, S.TIMED_OUT}),
    S.FAILED: frozenset({S.QUEUED}),
    S.TIMED_OUT: frozenset({S.QUEUED}),
    S.CANCELLED: frozenset({S.QUEUED}),
    S.SUCCEEDED: frozenset(),
    S.SKIPPED: frozenset(),
}


def can_transition(
    table: Mapping[RunStatus, frozenset[RunStatus]], current: RunStatus, target: RunStatus
) -> bool:
    return target in table.get(current, frozenset())


def check_transition(
    kind: str,
    table: Mapping[RunStatus, frozenset[RunStatus]],
    current: RunStatus | str,
    target: RunStatus | str,
) -> None:
    """Raise :class:`InvalidTransitionError` unless ``current -> target`` is allowed."""
    cur = RunStatus(current)
    tgt = RunStatus(target)
    if not can_transition(table, cur, tgt):
        raise InvalidTransitionError(kind, cur, tgt)


def check_run(current: RunStatus | str, target: RunStatus | str) -> None:
    check_transition("run", RUN_TRANSITIONS, current, target)


def check_attempt(current: RunStatus | str, target: RunStatus | str) -> None:
    check_transition("attempt", ATTEMPT_TRANSITIONS, current, target)


def check_step(current: RunStatus | str, target: RunStatus | str) -> None:
    check_transition("step", STEP_TRANSITIONS, current, target)
