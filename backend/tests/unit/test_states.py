from __future__ import annotations

import itertools

import pytest

from agenttape.domain import states
from agenttape.domain.enums import RunStatus
from agenttape.domain.errors import InvalidTransitionError

S = RunStatus
ALL = list(RunStatus)
TABLES = {
    "run": states.RUN_TRANSITIONS,
    "attempt": states.ATTEMPT_TRANSITIONS,
    "step": states.STEP_TRANSITIONS,
}


@pytest.mark.parametrize("kind", TABLES)
def test_every_status_has_an_entry(kind: str) -> None:
    assert set(TABLES[kind]) == set(ALL)


@pytest.mark.parametrize(
    ("kind", "current", "target"),
    [(k, c, t) for k in TABLES for c, t in itertools.product(ALL, ALL)],
)
def test_transition_table_is_enforced(kind: str, current: RunStatus, target: RunStatus) -> None:
    table = TABLES[kind]
    allowed = target in table[current]
    if allowed:
        states.check_transition(kind, table, current, target)
    else:
        with pytest.raises(InvalidTransitionError) as info:
            states.check_transition(kind, table, current, target)
        assert info.value.details == {"kind": kind, "from": current.value, "to": target.value}


def test_run_happy_path() -> None:
    path = [S.QUEUED, S.PREPARING, S.RUNNING, S.SUCCEEDED]
    for a, b in itertools.pairwise(path):
        states.check_run(a, b)


def test_run_approval_pause_and_resume() -> None:
    for a, b in [(S.RUNNING, S.WAITING_FOR_APPROVAL), (S.WAITING_FOR_APPROVAL, S.QUEUED)]:
        states.check_run(a, b)
    states.check_attempt(S.WAITING_FOR_APPROVAL, S.RUNNING)


def test_succeeded_and_skipped_are_final() -> None:
    for final in (S.SUCCEEDED, S.SKIPPED):
        assert states.RUN_TRANSITIONS[final] == frozenset()
        assert states.ATTEMPT_TRANSITIONS[final] == frozenset()


def test_failures_only_reopen_through_queue() -> None:
    for status in (S.FAILED, S.TIMED_OUT, S.CANCELLED):
        assert states.RUN_TRANSITIONS[status] == frozenset({S.QUEUED})
        assert states.ATTEMPT_TRANSITIONS[status] == frozenset()


@pytest.mark.parametrize(
    "bad", [(S.QUEUED, S.SUCCEEDED), (S.SUCCEEDED, S.RUNNING), (S.CANCELLED, S.RUNNING)]
)
def test_notable_invalid_run_transitions(bad: tuple[RunStatus, RunStatus]) -> None:
    with pytest.raises(InvalidTransitionError):
        states.check_run(*bad)


def test_accepts_string_statuses() -> None:
    states.check_run("queued", "preparing")
    with pytest.raises(ValueError):
        states.check_run("queued", "exploded")
