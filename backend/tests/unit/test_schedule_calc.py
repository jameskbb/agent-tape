"""Next-run calculation for one-time, interval, and cron schedules, including DST."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

import pytest

from agenttape.domain.errors import ValidationError
from agenttape.scheduler.calc import (
    CronCalculator,
    IntervalCalculator,
    OneTimeCalculator,
    Window,
    build_calculator,
    is_ambiguous,
    is_nonexistent,
    load_zone,
    resolve_local,
    to_utc,
)

NY = ZoneInfo("America/New_York")
LONDON = ZoneInfo("Europe/London")


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)  # type: ignore[misc]


# --------------------------------------------------------------------------- one-time


def test_one_time_fires_once_then_finishes() -> None:
    calc = OneTimeCalculator(utc(2026, 5, 1, 9))
    assert calc.next_after(utc(2026, 4, 30)) == utc(2026, 5, 1, 9)
    assert calc.next_after(utc(2026, 5, 1, 9)) is None
    assert calc.preview(utc(2026, 1, 1), 5) == [utc(2026, 5, 1, 9)]


def test_one_time_respects_end_window() -> None:
    calc = OneTimeCalculator(utc(2026, 5, 1, 9), Window(end_at=utc(2026, 4, 1)))
    assert calc.next_after(utc(2026, 1, 1)) is None


# --------------------------------------------------------------------------- interval


def test_interval_steps_from_anchor() -> None:
    calc = IntervalCalculator(3600, utc(2026, 1, 1, 0, 0))
    assert calc.next_after(utc(2026, 1, 1, 0, 0)) == utc(2026, 1, 1, 1)
    assert calc.next_after(utc(2026, 1, 1, 0, 30)) == utc(2026, 1, 1, 1)
    assert calc.next_after(utc(2025, 12, 31)) == utc(2026, 1, 1, 0, 0)
    assert calc.preview(utc(2026, 1, 1, 5, 59), 3) == [
        utc(2026, 1, 1, 6),
        utc(2026, 1, 1, 7),
        utc(2026, 1, 1, 8),
    ]


def test_interval_is_elapsed_time_across_dst() -> None:
    # 24h interval anchored at local midnight: after spring-forward the wall time shifts
    # by an hour because intervals measure elapsed time, not the clock on the wall.
    anchor = resolve_local(datetime(2026, 3, 7, 0, 0), NY)
    calc = IntervalCalculator(24 * 3600, anchor)
    nxt = calc.preview(anchor, 2)
    assert nxt[1] - nxt[0] == timedelta(hours=24)
    assert nxt[1].astimezone(NY).hour == 1


def test_interval_is_elapsed_time_across_fall_back() -> None:
    # 24h anchored at local midnight before 1 Nov 2026: clocks fall back an hour, so the
    # second step lands at 23:00 the same local day (25 wall-clock hours never happen).
    anchor = resolve_local(datetime(2026, 10, 31, 0, 0), NY)
    calc = IntervalCalculator(24 * 3600, anchor)
    runs = calc.preview(anchor, 2)
    assert runs[1] - runs[0] == timedelta(hours=24)
    assert [r.astimezone(NY).isoformat() for r in runs] == [
        "2026-11-01T00:00:00-04:00",
        "2026-11-01T23:00:00-05:00",
    ]


def test_interval_steps_through_the_repeated_hour() -> None:
    # A 30-minute interval visits 01:00 and 01:30 twice on 1 Nov 2026 (EDT, then EST).
    calc = IntervalCalculator(1800, utc(2026, 11, 1, 4, 30))
    runs = calc.preview(utc(2026, 11, 1, 4, 30), 5)
    assert [r.astimezone(NY).strftime("%H:%M%z") for r in runs] == [
        "01:00-0400",
        "01:30-0400",
        "01:00-0500",
        "01:30-0500",
        "02:00-0500",
    ]
    assert all(b - a == timedelta(minutes=30) for a, b in pairwise(runs))


def test_interval_window_end() -> None:
    calc = IntervalCalculator(600, utc(2026, 1, 1), Window(end_at=utc(2026, 1, 1, 0, 25)))
    assert calc.preview(utc(2026, 1, 1), 10) == [utc(2026, 1, 1, 0, 10), utc(2026, 1, 1, 0, 20)]


# --------------------------------------------------------------------------- cron


def test_cron_daily_in_zone() -> None:
    calc = CronCalculator("0 9 * * *", LONDON)
    # Winter: 09:00 GMT == 09:00 UTC; summer: 09:00 BST == 08:00 UTC.
    assert calc.next_after(utc(2026, 1, 10, 12)) == utc(2026, 1, 11, 9)
    assert calc.next_after(utc(2026, 7, 10, 12)) == utc(2026, 7, 11, 8)


def test_cron_weekdays_only() -> None:
    calc = CronCalculator("30 8 * * 1-5", NY)
    runs = calc.preview(utc(2026, 5, 1, 20), 3)  # Friday evening UTC
    assert [r.astimezone(NY).strftime("%a %H:%M") for r in runs] == [
        "Mon 08:30",
        "Tue 08:30",
        "Wed 08:30",
    ]


def test_cron_spring_forward_gap_fires_at_first_instant_after_gap() -> None:
    # 2026-03-08: New York clocks jump 02:00 EST -> 03:00 EDT; 02:30 does not exist.
    calc = CronCalculator("30 2 * * *", NY)
    runs = calc.preview(utc(2026, 3, 6, 12), 3)
    local = [r.astimezone(NY) for r in runs]
    assert local[0].isoformat() == "2026-03-07T02:30:00-05:00"
    assert local[1].isoformat() == "2026-03-08T03:00:00-04:00"  # shifted, not skipped
    assert local[2].isoformat() == "2026-03-09T02:30:00-04:00"


def test_cron_gap_times_collapse_without_duplicates() -> None:
    calc = CronCalculator("*/20 * * * *", NY)
    runs = calc.preview(utc(2026, 3, 8, 6, 30), 4)  # 01:30 EST
    assert [r.astimezone(NY).strftime("%H:%M") for r in runs] == [
        "01:40",
        "03:00",
        "03:20",
        "03:40",
    ]
    assert len(set(runs)) == len(runs)


def test_cron_fall_back_overlap_fires_once() -> None:
    # 2026-11-01: 01:00-02:00 happens twice in New York. A 01:30 job runs once (first pass).
    calc = CronCalculator("30 1 * * *", NY)
    runs = calc.preview(utc(2026, 10, 31, 12), 3)
    local = [r.astimezone(NY).isoformat() for r in runs]
    assert local == [
        "2026-11-01T01:30:00-04:00",
        "2026-11-02T01:30:00-05:00",
        "2026-11-03T01:30:00-05:00",
    ]


def test_cron_overlap_starting_inside_second_pass_does_not_repeat() -> None:
    calc = CronCalculator("*/30 * * * *", NY)
    # 06:15 UTC is 01:15 EST, the second pass through 01:xx.
    nxt = calc.next_after(utc(2026, 11, 1, 6, 15))
    assert nxt is not None
    assert nxt.astimezone(NY).isoformat() == "2026-11-01T02:00:00-05:00"


def test_cron_start_and_end_window() -> None:
    calc = CronCalculator(
        "0 * * * *", ZoneInfo("UTC"), Window(start_at=utc(2026, 1, 1, 5), end_at=utc(2026, 1, 1, 7))
    )
    assert calc.preview(utc(2026, 1, 1), 5) == [
        utc(2026, 1, 1, 5),
        utc(2026, 1, 1, 6),
        utc(2026, 1, 1, 7),
    ]


# --------------------------------------------------------------------------- helpers


def test_local_resolution_helpers() -> None:
    assert is_nonexistent(datetime(2026, 3, 8, 2, 30), NY)
    assert not is_nonexistent(datetime(2026, 3, 8, 3, 30), NY)
    assert is_ambiguous(datetime(2026, 11, 1, 1, 30), NY)
    assert resolve_local(datetime(2026, 3, 8, 2, 30), NY) == utc(2026, 3, 8, 7)
    assert resolve_local(datetime(2026, 11, 1, 1, 30), NY) == utc(2026, 11, 1, 5, 30)


def test_naive_inputs_are_local_wall_time() -> None:
    assert to_utc(datetime(2026, 7, 1, 9, 0), LONDON) == utc(2026, 7, 1, 8)
    assert to_utc(datetime(2026, 7, 1, 9, 0, tzinfo=UTC), LONDON) == utc(2026, 7, 1, 9)


@pytest.mark.parametrize("zone", ["", "Mars/Olympus", "not a zone", " UTC"])
def test_invalid_zone_rejected(zone: str) -> None:
    with pytest.raises(ValidationError):
        load_zone(zone)


@pytest.mark.parametrize("expr", ["", "* * * *", "61 * * * *", "* * * * * *", "hello"])
def test_invalid_cron_rejected(expr: str) -> None:
    with pytest.raises(ValidationError):
        build_calculator(kind="cron", timezone="UTC", cron_expression=expr)


def test_build_validates_each_kind() -> None:
    with pytest.raises(ValidationError):
        build_calculator(kind="one_time", timezone="UTC")
    with pytest.raises(ValidationError):
        build_calculator(
            kind="interval", timezone="UTC", interval_seconds=5, anchor=utc(2026, 1, 1)
        )
    with pytest.raises(ValidationError):
        build_calculator(kind="weekly", timezone="UTC")
    with pytest.raises(ValidationError):
        build_calculator(
            kind="cron",
            timezone="UTC",
            cron_expression="* * * * *",
            start_at=utc(2026, 1, 2),
            end_at=utc(2026, 1, 1),
        )
    calc = build_calculator(
        kind="interval", timezone="UTC", interval_seconds=60, anchor=utc(2026, 1, 1)
    )
    assert calc.next_after(utc(2026, 1, 1)) == utc(2026, 1, 1, 0, 1)
