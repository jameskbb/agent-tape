import { lazy, Suspense, useState } from "react";
import { Link } from "react-router";

import { StartDemoRun } from "@/components/demo/StartDemoRun";
import { Page } from "@/components/layout/Page";
import {
  Card,
  Duration,
  EmptyState,
  ErrorState,
  LoadingBlock,
  RelativeTime,
  Skeleton,
  Stat,
  StatusPill,
  Unavailable,
  WindowSwitch,
} from "@/components/ui";
import { VerifiedFindingsStat } from "@/features/findings/VerifiedFindingsStat";
import { OnboardingBanner } from "@/features/onboarding/OnboardingBanner";
import { formatDuration, humanize } from "@/lib/format";

import { useDashboard, type Dashboard } from "./api";

const ActivityChart = lazy(() => import("./ActivityChart"));

function Stats({ d }: { d: Dashboard }) {
  const rate = d.success_rate.rate;
  const tokens = d.usage.total_tokens;
  const cost = d.cost.total_usd;
  return (
    <dl className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-5">
      <VerifiedFindingsStat tally={d.findings} days={d.window_days} />
      <Stat
        label="Success rate"
        value={
          rate === null ? <Unavailable reason="No finished runs" /> : `${Math.round(rate * 100)}%`
        }
        detail={
          d.success_rate.finished
            ? `${d.success_rate.succeeded} of ${d.success_rate.finished} finished runs; cancelled runs are not counted`
            : undefined
        }
      />
      <Stat
        label="Median duration"
        value={
          d.duration.median_ms === null ? (
            <Unavailable reason="No finished runs" />
          ) : (
            formatDuration(d.duration.median_ms)
          )
        }
        detail={
          d.duration.p90_ms !== null
            ? `90% finish within ${formatDuration(d.duration.p90_ms) ?? "?"}`
            : undefined
        }
      />
      <Stat
        label="Runs"
        value={d.runs_in_window}
        detail={`Started in the last ${d.window_days} days`}
      />
      <Stat
        label="Token usage and cost"
        value={tokens === null ? <Unavailable reason="Not reported" /> : tokens.toLocaleString()}
        detail={
          <>
            {tokens !== null
              ? `Reported by ${d.usage.runs_reporting} run${d.usage.runs_reporting === 1 ? "" : "s"}. `
              : "No adapter reported real usage. "}
            {cost !== null ? `$${cost.toFixed(2)} reported cost. ` : "Cost not reported. "}
            {d.usage.simulated_runs
              ? `${d.usage.simulated_runs} demo run${d.usage.simulated_runs === 1 ? "" : "s"} with simulated usage not counted.`
              : null}
          </>
        }
      />
    </dl>
  );
}

function RunRows({ runs, empty }: { runs: Dashboard["live_runs"]; empty: string }) {
  if (runs.length === 0) return <p className="text-sm text-muted">{empty}</p>;
  return (
    <ul className="divide-y divide-line">
      {runs.map((r) => (
        <li key={r.id} className="flex flex-wrap items-center gap-3 py-2.5">
          <StatusPill status={r.status} size="sm" />
          <Link
            to={`/runs/${r.id}`}
            className="min-w-0 flex-1 truncate font-medium hover:text-accent"
          >
            {r.title}
          </Link>
          <span className="text-sm text-muted">
            {r.failure_category ? (
              humanize(r.failure_category)
            ) : r.started_at && !r.finished_at ? (
              <Duration start={r.started_at} live />
            ) : (
              <RelativeTime iso={r.created_at} />
            )}
          </span>
        </li>
      ))}
    </ul>
  );
}

function ActivityCard({ d }: { d: Dashboard }) {
  const [asTable, setAsTable] = useState(false);
  const total = d.activity.reduce((n, a) => n + a.succeeded + a.failed + a.other, 0);
  return (
    <Card
      title="Activity"
      description={`Finished runs per day (UTC), last ${d.window_days} days`}
      actions={
        <button
          type="button"
          onClick={() => setAsTable(!asTable)}
          aria-pressed={asTable}
          className="text-sm text-accent hover:underline"
        >
          {asTable ? "Show chart" : "Show as table"}
        </button>
      }
    >
      <ul className="mb-3 flex flex-wrap gap-4 text-sm text-ink-soft" aria-label="Legend">
        <li className="flex items-center gap-1.5">
          <span aria-hidden className="size-3 rounded-[2px] bg-[var(--chart-good)]" />
          Succeeded
        </li>
        <li className="flex items-center gap-1.5">
          <span
            aria-hidden
            className="size-3 rounded-[2px] bg-[repeating-linear-gradient(45deg,var(--chart-bad)_0_3px,color-mix(in_srgb,var(--chart-bad)_55%,var(--surface))_3px_5px)]"
          />
          Failed or timed out
        </li>
        <li className="flex items-center gap-1.5">
          <span aria-hidden className="size-3 rounded-[2px] bg-[var(--chart-other)]" />
          Cancelled or skipped
        </li>
      </ul>
      {total === 0 ? (
        <p className="py-8 text-center text-sm text-muted">No runs finished in this window.</p>
      ) : asTable ? (
        <div className="max-h-64 overflow-auto">
          <table className="w-full text-left text-sm tabular-nums">
            <caption className="sr-only">Runs per day by outcome</caption>
            <thead className="text-muted">
              <tr>
                <th scope="col" className="py-1 font-medium">
                  Day
                </th>
                <th scope="col" className="py-1 text-right font-medium">
                  Succeeded
                </th>
                <th scope="col" className="py-1 text-right font-medium">
                  Failed
                </th>
                <th scope="col" className="py-1 text-right font-medium">
                  Other
                </th>
              </tr>
            </thead>
            <tbody>
              {d.activity.map((a) => (
                <tr key={a.date} className="border-t border-line">
                  <td className="py-1">{a.date}</td>
                  <td className="py-1 text-right">{a.succeeded}</td>
                  <td className="py-1 text-right">{a.failed}</td>
                  <td className="py-1 text-right">{a.other}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <Suspense fallback={<Skeleton className="h-64" />}>
          <ActivityChart data={d.activity} />
        </Suspense>
      )}
    </Card>
  );
}

function FailureCategories({ d }: { d: Dashboard }) {
  const max = Math.max(1, ...d.failure_categories.map((c) => c.count));
  return (
    <Card
      title="Top failure categories"
      description={`Failed and timed-out runs, last ${d.window_days} days`}
    >
      {d.failure_categories.length === 0 ? (
        <p className="text-sm text-muted">No failures in this window.</p>
      ) : (
        <ol className="flex flex-col gap-3">
          {d.failure_categories.map((c) => (
            <li key={c.category}>
              <Link to={`/runs?status=failed&status=timed_out`} className="group block">
                <span className="flex justify-between text-sm">
                  <span className="text-ink group-hover:text-accent">{humanize(c.category)}</span>
                  <span className="text-ink-soft tabular-nums">{c.count}</span>
                </span>
                <span aria-hidden className="mt-1 block h-2 rounded-full bg-sunken">
                  <span
                    className="block h-2 rounded-full bg-[var(--chart-bar)]"
                    style={{ width: `${(c.count / max) * 100}%` }}
                  />
                </span>
              </Link>
            </li>
          ))}
        </ol>
      )}
    </Card>
  );
}

/** Overview dashboard: live work, what needs you, outcomes, and activity. */
export function OverviewPage() {
  const [days, setDays] = useState<number>(14);
  const dash = useDashboard(days);

  if (dash.isPending) {
    return (
      <Page title="Overview">
        <LoadingBlock />
      </Page>
    );
  }
  if (dash.isError) {
    return (
      <Page title="Overview">
        <ErrorState error={dash.error} onRetry={() => void dash.refetch()} />
      </Page>
    );
  }
  const d = dash.data;
  const liveTotal = Object.values(d.live).reduce((a, b) => a + b, 0);
  if (
    d.runs_in_window === 0 &&
    liveTotal === 0 &&
    d.recent_failures.length === 0 &&
    d.pending_approval_count === 0
  ) {
    return (
      <Page
        title="Welcome to AgentTape"
        description="A flight recorder and control plane for AI coding agents on your machine."
      >
        <OnboardingBanner />
        <EmptyState
          title="Record your first run"
          description="The built-in Demo Agent needs no credentials. It makes a small, real change in a repository AgentTape owns, so you can watch events stream in, then inspect the commands, diff, artifacts, and evaluations it leaves behind."
          action={<StartDemoRun />}
        />
      </Page>
    );
  }
  const liveSummary = Object.entries(d.live)
    .filter(([, n]) => n > 0)
    .map(([s, n]) => `${n} ${humanize(s).toLowerCase()}`)
    .join(", ");

  return (
    <Page
      title="Overview"
      actions={
        <>
          <WindowSwitch value={days} onChange={setDays} />
          <StartDemoRun variant="secondary" />
        </>
      }
    >
      <OnboardingBanner />
      <Stats d={d} />
      <div className="grid gap-4 lg:grid-cols-2">
        <Card
          title="Happening now"
          description={liveSummary || "Nothing is queued, running, or paused."}
        >
          <RunRows runs={d.live_runs} empty="Start a run and it appears here while it works." />
        </Card>
        <Card
          title="Waiting for you"
          description="Approvals that block a run until someone decides."
          actions={
            <Link to="/approvals" className="text-sm text-accent hover:underline">
              Approval inbox
            </Link>
          }
        >
          {d.pending_approvals.length === 0 ? (
            <p className="text-sm text-muted">No approvals are waiting.</p>
          ) : (
            <ul className="divide-y divide-line">
              {d.pending_approvals.map((a) => (
                <li key={a.id} className="py-2.5">
                  {a.run_id ? (
                    <Link to={`/runs/${a.run_id}`} className="font-medium hover:text-accent">
                      {a.title}
                    </Link>
                  ) : (
                    <span className="font-medium">{a.title}</span>
                  )}
                  <p className="text-sm text-muted">
                    {humanize(a.risk_level)} risk, requested <RelativeTime iso={a.created_at} />
                  </p>
                </li>
              ))}
              {d.pending_approval_count > d.pending_approvals.length ? (
                <li className="py-2.5 text-sm text-muted">
                  {d.pending_approval_count - d.pending_approvals.length} more in the inbox
                </li>
              ) : null}
            </ul>
          )}
        </Card>
      </div>
      <div className="grid gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <ActivityCard d={d} />
        <FailureCategories d={d} />
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <Card
          title="Recent failures"
          actions={
            <Link
              to="/runs?status=failed&status=timed_out"
              className="text-sm text-accent hover:underline"
            >
              All failures
            </Link>
          }
        >
          <RunRows runs={d.recent_failures} empty="No failed or timed-out runs." />
        </Card>
        <Card
          title="Upcoming schedules"
          description={d.enabled_schedule_count ? `${d.enabled_schedule_count} enabled` : undefined}
          actions={
            <Link to="/schedules" className="text-sm text-accent hover:underline">
              Schedules
            </Link>
          }
        >
          {d.upcoming_schedules.length === 0 ? (
            <p className="text-sm text-muted">
              No enabled schedules. Seeded schedules start paused; enable one on the Schedules page.
            </p>
          ) : (
            <ul className="divide-y divide-line">
              {d.upcoming_schedules.map((s) => (
                <li key={s.id} className="flex flex-wrap items-center justify-between gap-2 py-2.5">
                  <Link to={`/schedules/${s.id}`} className="font-medium hover:text-accent">
                    {s.name}
                  </Link>
                  <span className="text-sm text-muted">
                    <RelativeTime iso={s.next_run_at} /> ({s.timezone})
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </Page>
  );
}
