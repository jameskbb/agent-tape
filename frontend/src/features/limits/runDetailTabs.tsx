/** Run-detail "Limits" tab: effective budget with live checks, timeouts, retry policy,
 * and the concurrency scopes that apply to the run. */
import { TERMINAL_STATUSES, type RunDetail } from "@/api/types";
import type { RunDetailTab } from "@/app/registry";
import { Badge, ErrorState, LoadingBlock, Unavailable } from "@/components/ui";
import { cn } from "@/lib/cn";
import { formatBytes, humanize } from "@/lib/format";
import { useRunLimits, type BudgetCheck } from "@/features/schedules/api";

const STATUS_TONE: Record<BudgetCheck["status"], "success" | "warning" | "danger" | "neutral"> = {
  ok: "success",
  warning: "warning",
  exceeded: "danger",
  not_applicable: "neutral",
  unknown: "neutral",
};

const STATUS_LABEL: Record<BudgetCheck["status"], string> = {
  ok: "Within budget",
  warning: "Near the limit",
  exceeded: "Exceeded",
  not_applicable: "Not counted",
  unknown: "Not reported",
};

function amount(value: number, unit: string): string {
  if (unit === "bytes") return formatBytes(value);
  if (unit === "USD") return `$${value.toFixed(value < 1 ? 4 : 2)}`;
  return `${Number.isInteger(value) ? value : value.toFixed(2)} ${unit}`;
}

function Meter({ check }: { check: BudgetCheck }) {
  const pct = Math.min(100, Math.round((check.ratio ?? 0) * 100));
  return (
    <div
      role="meter"
      aria-label={`${check.label} used`}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={pct}
      className="h-1.5 w-full overflow-hidden rounded-full bg-sunken"
    >
      <div
        className={cn(
          "h-full rounded-full",
          check.status === "exceeded"
            ? "bg-[var(--st-failed)]"
            : check.status === "warning"
              ? "bg-[var(--st-cancelling)]"
              : "bg-[var(--st-succeeded)]",
        )}
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

function LimitsTab({ detail }: { detail: RunDetail }) {
  const live = !TERMINAL_STATUSES.has(detail.run.status);
  const limits = useRunLimits(detail.run.id, live);
  if (limits.isPending) return <LoadingBlock label="Loading limits" />;
  if (limits.isError)
    return <ErrorState error={limits.error} onRetry={() => void limits.refetch()} />;
  const l = limits.data;
  const configuredRetry = Object.keys(l.retry.configured).length > 0;
  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <section aria-labelledby="budget-heading" className="flex flex-col gap-3">
        <h3 id="budget-heading" className="font-semibold">
          Budget
        </h3>
        {l.checks.length === 0 ? (
          <p className="text-sm text-muted">
            No budget applies to this run. Set defaults in Settings, Limits, or a budget on the
            project or agent profile.
          </p>
        ) : (
          <ul className="flex flex-col gap-3">
            {l.checks.map((c) => (
              <li key={c.key} className="flex flex-col gap-1.5">
                <div className="flex flex-wrap items-baseline justify-between gap-2 text-sm">
                  <span className="font-medium">{c.label}</span>
                  <span className="flex items-center gap-2">
                    {c.used === null ? (
                      <Unavailable reason={c.note ?? "Not reported"} />
                    ) : (
                      <span className="tabular-nums">
                        {amount(c.used, c.unit)} of {amount(c.limit, c.unit)}
                      </span>
                    )}
                    <Badge tone={STATUS_TONE[c.status]}>{STATUS_LABEL[c.status]}</Badge>
                  </span>
                </div>
                {c.used !== null ? <Meter check={c} /> : null}
                <span className="text-xs text-muted">
                  Limit from {l.budget_sources[c.key] ?? "the run"}
                </span>
              </li>
            ))}
          </ul>
        )}
        {l.checks.length ? (
          <p className="text-xs text-muted">
            A warning is recorded at {Math.round((l.budget.warn_ratio ?? 0.8) * 100)}% of a limit;
            passing a limit stops the run with a budget exceeded result.
          </p>
        ) : null}
      </section>

      <section aria-labelledby="time-heading" className="flex flex-col gap-3">
        <h3 id="time-heading" className="font-semibold">
          Time, attempts, and retries
        </h3>
        <dl className="grid grid-cols-2 gap-3 text-sm">
          <div>
            <dt className="text-xs text-muted">Wall-clock limit</dt>
            <dd>{l.timeout_seconds ? `${l.timeout_seconds} s` : <Unavailable reason="None" />}</dd>
          </div>
          <div>
            <dt className="text-xs text-muted">Idle limit</dt>
            <dd>
              {l.idle_timeout_seconds ? (
                `${l.idle_timeout_seconds} s`
              ) : (
                <Unavailable reason="None" />
              )}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-muted">Attempts</dt>
            <dd>
              {l.attempt_count} used of {l.max_attempts}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-muted">Retry waits</dt>
            <dd>
              {l.retry.delays_seconds.length
                ? l.retry.delays_seconds.map((d) => `${d.toFixed(d < 10 ? 1 : 0)} s`).join(", ")
                : "No automatic retry"}
            </dd>
          </div>
          <div className="col-span-2">
            <dt className="text-xs text-muted">
              Retried automatically after{" "}
              {configuredRetry ? "(from the agent profile)" : "(defaults)"}
            </dt>
            <dd className="mt-1 flex flex-wrap gap-1">
              {l.retry.retry_on.map((c) => (
                <Badge key={c}>{humanize(c)}</Badge>
              ))}
            </dd>
          </div>
        </dl>
        <h3 className="mt-2 font-semibold">Running at the same time</h3>
        {l.concurrency.length === 0 ? (
          <p className="text-sm text-muted">No concurrency limit applies to this run.</p>
        ) : (
          <ul className="flex flex-col gap-1 text-sm">
            {l.concurrency.map((s) => (
              <li key={s.scope} className="flex justify-between gap-2">
                <span>{s.label}</span>
                <span className="tabular-nums text-ink-soft">
                  {s.running} of {s.limit} running
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

export const runDetailTabs: RunDetailTab[] = [
  {
    id: "limits",
    label: "Limits",
    order: 75,
    render: ({ detail }) => <LimitsTab detail={detail} />,
  },
];
