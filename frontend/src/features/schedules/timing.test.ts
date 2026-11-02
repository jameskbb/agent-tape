import { describe, expect, it } from "vitest";

import { describeTiming, formatLocal, splitInterval, splitLocalIso, toLocalInput } from "./timing";

describe("schedule timing helpers", () => {
  it("reads wall-clock parts in the schedule's zone, not the browser's", () => {
    const p = splitLocalIso("2026-11-01T01:30:00-04:00");
    expect(p).toMatchObject({
      date: "2026-11-01",
      time: "01:30",
      offset: "-04:00",
      weekday: "Sun",
    });
    expect(formatLocal("2026-03-08T03:00:00-04:00")).toBe("Sun 8 Mar 2026, 03:00");
    expect(toLocalInput("2026-09-24T02:30:00+01:00")).toBe("2026-09-24T02:30");
    expect(splitLocalIso("not a date")).toBeNull();
    expect(formatLocal(null)).toBeNull();
  });

  it("splits intervals into the largest whole unit", () => {
    expect(splitInterval(7200)).toEqual({ value: 2, unit: 3600 });
    expect(splitInterval(86400 * 3)).toEqual({ value: 3, unit: 86400 });
    expect(splitInterval(90 * 60)).toEqual({ value: 90, unit: 60 });
  });

  it("describes timing in plain words", () => {
    expect(
      describeTiming({
        kind: "cron",
        cron_expression: "0 2 * * *",
        interval_seconds: null,
        run_at_local: null,
      }),
    ).toBe("Every day at 02:00");
    expect(
      describeTiming({
        kind: "cron",
        cron_expression: "*/5 * * * *",
        interval_seconds: null,
        run_at_local: null,
      }),
    ).toBe("Cron */5 * * * *");
    expect(
      describeTiming({
        kind: "interval",
        cron_expression: null,
        interval_seconds: 3600,
        run_at_local: null,
      }),
    ).toBe("Every hour");
    expect(
      describeTiming({
        kind: "interval",
        cron_expression: null,
        interval_seconds: 1800,
        run_at_local: null,
      }),
    ).toBe("Every 30 minutes");
    expect(
      describeTiming({
        kind: "one_time",
        cron_expression: null,
        interval_seconds: null,
        run_at_local: "2026-10-01T09:00:00+02:00",
      }),
    ).toBe("Once, Thu 1 Oct 2026, 09:00");
  });
});
