/**
 * Pure helpers for schedule timing. Local times arrive from the API as ISO strings with
 * the schedule zone's offset ("2026-11-01T01:30:00-04:00"); we read their wall-clock parts
 * directly instead of converting to the browser's zone, so the UI always shows the time in
 * the schedule's own zone.
 */
import type { ConcurrencyPolicy, MisfirePolicy, Schedule, ScheduleKind } from "./api";

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export interface LocalParts {
  date: string;
  time: string;
  offset: string;
  weekday: string;
  day: number;
  month: string;
  year: number;
}

export function splitLocalIso(iso: string): LocalParts | null {
  const m =
    /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::\d{2}(?:\.\d+)?)?(Z|[+-]\d{2}:\d{2})?$/.exec(iso);
  if (!m) return null;
  const [, y, mo, d, h, mi, off] = m;
  const year = Number(y);
  const monthIndex = Number(mo) - 1;
  const day = Number(d);
  const weekday = WEEKDAYS[new Date(Date.UTC(year, monthIndex, day)).getUTCDay()] ?? "";
  return {
    date: `${y}-${mo}-${d}`,
    time: `${h}:${mi}`,
    offset: off === "Z" || off === undefined ? "+00:00" : off,
    weekday,
    day,
    month: MONTHS[monthIndex] ?? "",
    year,
  };
}

/** "Thu 24 Sep 2026, 02:30" in the schedule's zone. */
export function formatLocal(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const p = splitLocalIso(iso);
  if (!p) return iso;
  return `${p.weekday} ${p.day} ${p.month} ${p.year}, ${p.time}`;
}

/** Value for an `<input type="datetime-local">` from a local ISO string. */
export function toLocalInput(iso: string | null | undefined): string {
  if (!iso) return "";
  const p = splitLocalIso(iso);
  return p ? `${p.date}T${p.time}` : "";
}

export const CRON_PRESETS: { label: string; expression: string }[] = [
  { label: "Every hour", expression: "0 * * * *" },
  { label: "Every day at 02:00", expression: "0 2 * * *" },
  { label: "Weekdays at 09:00", expression: "0 9 * * 1-5" },
  { label: "Mondays at 08:30", expression: "30 8 * * 1" },
  { label: "First of the month at 06:00", expression: "0 6 1 * *" },
];

export const INTERVAL_UNITS: { label: string; seconds: number }[] = [
  { label: "minutes", seconds: 60 },
  { label: "hours", seconds: 3600 },
  { label: "days", seconds: 86400 },
];

export function splitInterval(seconds: number | null | undefined): { value: number; unit: number } {
  if (!seconds || seconds <= 0) return { value: 1, unit: 3600 };
  for (const u of [...INTERVAL_UNITS].reverse()) {
    if (seconds % u.seconds === 0) return { value: seconds / u.seconds, unit: u.seconds };
  }
  return { value: Math.round(seconds / 60), unit: 60 };
}

export function describeInterval(seconds: number): string {
  const { value, unit } = splitInterval(seconds);
  const name = INTERVAL_UNITS.find((u) => u.seconds === unit)?.label ?? "minutes";
  return value === 1 ? `Every ${name.replace(/s$/, "")}` : `Every ${value} ${name}`;
}

export function describeTiming(
  s: Pick<Schedule, "kind" | "cron_expression" | "interval_seconds" | "run_at_local">,
): string {
  if (s.kind === "cron") {
    const preset = CRON_PRESETS.find((p) => p.expression === s.cron_expression?.trim());
    return preset ? preset.label : `Cron ${s.cron_expression ?? ""}`.trim();
  }
  if (s.kind === "interval") return describeInterval(s.interval_seconds ?? 0);
  return `Once, ${formatLocal(s.run_at_local) ?? "time not set"}`;
}

export const KIND_LABELS: Record<ScheduleKind, string> = {
  cron: "Cron",
  interval: "Fixed interval",
  one_time: "One time",
};

export const MISFIRE_COPY: Record<MisfirePolicy, { label: string; detail: string }> = {
  run_once: {
    label: "Run once to catch up",
    detail:
      "If AgentTape was stopped when runs were due, start one run when it comes back and record the others as missed.",
  },
  skip: {
    label: "Skip missed runs",
    detail:
      "If AgentTape was stopped when runs were due, record them as missed and wait for the next scheduled time.",
  },
};

export const CONCURRENCY_COPY: Record<ConcurrencyPolicy, { label: string; detail: string }> = {
  queue: {
    label: "Wait for the previous run",
    detail:
      "If the last run is still going, the new one waits and starts when it finishes. Extra waiting runs beyond the limit are skipped.",
  },
  skip: {
    label: "Skip while a run is active",
    detail: "If the last run is still going, this occurrence is skipped and recorded in history.",
  },
  cancel_and_replace: {
    label: "Cancel and replace",
    detail: "If the last run is still going, it is cancelled and the new run starts.",
  },
};

export const OCCURRENCE_LABELS: Record<string, { label: string; tone: string }> = {
  created: { label: "Run created", tone: "success" },
  queued: { label: "Waiting", tone: "violet" },
  pending: { label: "Starting", tone: "accent" },
  skipped: { label: "Skipped", tone: "neutral" },
  missed: { label: "Missed", tone: "warning" },
  replaced: { label: "Replaced", tone: "warning" },
  failed: { label: "Could not start", tone: "danger" },
};

export function browserTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}
