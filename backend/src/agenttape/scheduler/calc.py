"""Occurrence calculators for one-time, fixed-interval, and cron schedules (pure).

Everything is stored and compared in UTC; the schedule's IANA zone is used only to turn
local wall-clock rules (cron, local start/end times) into UTC instants.

DST rules (documented in docs/SCHEDULING.md):

* **Cron** expressions match *local wall-clock* times in the schedule's zone.
  - A wall time that does not exist (spring-forward gap, e.g. 02:30 in New York on the
    second Sunday of March) fires at the first instant after the gap (03:00 local).
    Several gap times collapse into that one instant, so nothing fires twice.
  - A wall time that happens twice (fall-back overlap, e.g. 01:30 in New York on the
    first Sunday of November) fires once, at its first occurrence.
* **Fixed intervals** are elapsed time (``anchor + k * interval``); DST never shifts them.
* **One-time** schedules fire at a single UTC instant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from croniter import croniter

from agenttape.domain.errors import ValidationError

#: Upper bound on candidate wall times examined per ``next_after`` call (a minute-level cron
#: crossing a one-hour DST overlap needs ~60).
MAX_CRON_STEPS = 5000
MIN_INTERVAL_SECONDS = 60
MAX_INTERVAL_SECONDS = 366 * 24 * 3600
SCHEDULE_KINDS = ("one_time", "interval", "cron")


def load_zone(name: str) -> ZoneInfo:
    """Resolve an IANA zone name or raise a 422-style ValidationError."""
    if not name or name != name.strip():
        raise ValidationError("A valid IANA time zone is required", details={"timezone": name})
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationError(
            f"Unknown time zone '{name}'. Use an IANA name such as 'Europe/London'.",
            details={"timezone": name},
        ) from exc


def timezone_names() -> list[str]:
    return sorted(z for z in available_timezones() if "/" in z or z == "UTC")


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("naive datetime; use aware UTC")
    return value.astimezone(UTC)


def to_utc(value: datetime, zone: ZoneInfo) -> datetime:
    """Aware values are converted; naive values are local wall time in ``zone``."""
    if value.tzinfo is not None:
        return value.astimezone(UTC)
    return resolve_local(value, zone)


def is_nonexistent(naive: datetime, zone: ZoneInfo) -> bool:
    aware = naive.replace(tzinfo=zone, fold=0)
    return aware.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != naive


def is_ambiguous(naive: datetime, zone: ZoneInfo) -> bool:
    a = naive.replace(tzinfo=zone, fold=0).utcoffset()
    b = naive.replace(tzinfo=zone, fold=1).utcoffset()
    return a != b and not is_nonexistent(naive, zone)


def resolve_local(naive: datetime, zone: ZoneInfo) -> datetime:
    """Map a local wall time to one UTC instant using the DST rules above."""
    if not is_nonexistent(naive, zone):
        # fold=0 is the first occurrence of an ambiguous (repeated) wall time.
        return naive.replace(tzinfo=zone, fold=0).astimezone(UTC)
    # Inside a gap: the instant the clocks jumped. The two fold interpretations bracket it.
    lo = naive.replace(tzinfo=zone, fold=1).astimezone(UTC)
    hi = naive.replace(tzinfo=zone, fold=0).astimezone(UTC)
    lo, hi = min(lo, hi), max(lo, hi)
    before = lo.astimezone(zone).utcoffset()
    while (hi - lo) > timedelta(seconds=1):
        mid = lo + (hi - lo) / 2
        if mid.astimezone(zone).utcoffset() == before:
            lo = mid
        else:
            hi = mid
    return hi.replace(microsecond=0) if hi.microsecond else hi


@dataclass(frozen=True)
class Window:
    """Optional active window; occurrences outside it are never produced."""

    start_at: datetime | None = None
    end_at: datetime | None = None

    def clamp_after(self, after: datetime) -> datetime:
        """Occurrences must be > the returned instant (respects an inclusive start)."""
        if self.start_at is not None and after < self.start_at:
            return self.start_at - timedelta(microseconds=1)
        return after

    def allows(self, when: datetime) -> bool:
        if self.start_at is not None and when < self.start_at:
            return False
        return not (self.end_at is not None and when > self.end_at)


class _Base:
    window: Window

    def next_after(self, after: datetime) -> datetime | None:
        raise NotImplementedError

    def preview(self, after: datetime, count: int) -> list[datetime]:
        out: list[datetime] = []
        cursor = after
        for _ in range(max(0, count)):
            nxt = self.next_after(cursor)
            if nxt is None:
                break
            out.append(nxt)
            cursor = nxt
        return out


class OneTimeCalculator(_Base):
    def __init__(self, run_at: datetime, window: Window | None = None) -> None:
        self.run_at = ensure_utc(run_at)
        self.window = window or Window()

    def next_after(self, after: datetime) -> datetime | None:
        after = ensure_utc(after)
        if self.run_at > after and self.window.allows(self.run_at):
            return self.run_at
        return None


class IntervalCalculator(_Base):
    """``anchor + k * interval`` for k >= 0 (elapsed time; unaffected by DST)."""

    def __init__(self, interval_seconds: int, anchor: datetime, window: Window | None = None):
        if interval_seconds < 1:
            raise ValidationError("interval_seconds must be positive")
        self.interval = timedelta(seconds=interval_seconds)
        self.anchor = ensure_utc(anchor)
        self.window = window or Window()

    def next_after(self, after: datetime) -> datetime | None:
        after = self.window.clamp_after(ensure_utc(after))
        if after < self.anchor:
            candidate = self.anchor
        else:
            steps = (after - self.anchor) // self.interval + 1
            candidate = self.anchor + steps * self.interval
        return candidate if self.window.allows(candidate) else None


class CronCalculator(_Base):
    """Standard 5-field cron evaluated on local wall-clock time in ``zone``."""

    def __init__(self, expression: str, zone: ZoneInfo, window: Window | None = None) -> None:
        validate_cron(expression)
        self.expression = expression.strip()
        self.zone = zone
        self.window = window or Window()

    def next_after(self, after: datetime) -> datetime | None:
        after = self.window.clamp_after(ensure_utc(after))
        # Start from the local wall time of ``after``; candidates that resolve to an
        # instant at or before ``after`` (overlap repeats, collapsed gap times) are skipped.
        local = after.astimezone(self.zone).replace(tzinfo=None, fold=0)
        start = local - timedelta(hours=3)  # re-examine the overlap/gap region safely
        it = croniter(self.expression, start)
        for _ in range(MAX_CRON_STEPS):
            naive: datetime = it.get_next(datetime)
            candidate = resolve_local(naive, self.zone)
            if candidate <= after:
                continue
            if self.window.end_at is not None and candidate > self.window.end_at:
                return None
            return candidate
        return None


def validate_cron(expression: str) -> None:
    expr = (expression or "").strip()
    fields = expr.split()
    if len(fields) != 5:
        raise ValidationError(
            "Cron expressions need 5 fields: minute hour day-of-month month day-of-week",
            details={"cron_expression": expression},
        )
    if not croniter.is_valid(expr):
        raise ValidationError(
            f"'{expression}' is not a valid cron expression",
            details={"cron_expression": expression},
        )


def build_calculator(
    *,
    kind: str,
    timezone: str,
    cron_expression: str | None = None,
    interval_seconds: int | None = None,
    run_at: datetime | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    anchor: datetime | None = None,
) -> OneTimeCalculator | IntervalCalculator | CronCalculator:
    """Validate a schedule definition (all datetimes aware UTC) and build its calculator."""
    zone = load_zone(timezone)
    if start_at is not None and end_at is not None and end_at <= start_at:
        raise ValidationError("end_at must be after start_at")
    window = Window(start_at=start_at, end_at=end_at)
    if kind == "one_time":
        if run_at is None:
            raise ValidationError("run_at is required for a one-time schedule")
        return OneTimeCalculator(run_at, window)
    if kind == "interval":
        if interval_seconds is None:
            raise ValidationError("interval_seconds is required for an interval schedule")
        if not MIN_INTERVAL_SECONDS <= interval_seconds <= MAX_INTERVAL_SECONDS:
            raise ValidationError(
                f"interval_seconds must be between {MIN_INTERVAL_SECONDS} and {MAX_INTERVAL_SECONDS}",
                details={"interval_seconds": interval_seconds},
            )
        base = start_at or anchor
        if base is None:
            raise ValidationError("an interval schedule needs start_at or an anchor time")
        return IntervalCalculator(interval_seconds, base, window)
    if kind == "cron":
        if not cron_expression:
            raise ValidationError("cron_expression is required for a cron schedule")
        return CronCalculator(cron_expression, zone, window)
    raise ValidationError(
        f"Unknown schedule kind '{kind}'", details={"allowed": list(SCHEDULE_KINDS)}
    )


def local_note(when: datetime, zone: ZoneInfo, *, kind: str, cron_hint: bool = True) -> str | None:
    """Explain DST adjustments for a computed occurrence (used by the preview)."""
    if kind != "cron" or not cron_hint:
        return None
    local = when.astimezone(zone)
    prev = (when - timedelta(seconds=1)).astimezone(zone)
    if prev.utcoffset() != local.utcoffset() and local.utcoffset() > prev.utcoffset():  # type: ignore[operator]
        return "Clocks sprang forward; wall times in the skipped hour fire at this instant"
    naive = local.replace(tzinfo=None)
    if is_ambiguous(naive, zone):
        return "Clocks fall back after this; the repeated wall time fires only once"
    return None
