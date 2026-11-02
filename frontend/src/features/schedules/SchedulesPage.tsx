import { CalendarClock, Plus, Search, Webhook } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";

import { Page } from "@/components/layout/Page";
import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  ErrorState,
  LoadingBlock,
  RelativeTime,
  StatusPill,
  type Column,
} from "@/components/ui";

import { useSchedules, type Schedule } from "./api";
import { OccurrenceBadge, SchedulerStatusNote } from "./parts";
import { CONCURRENCY_COPY, describeTiming, formatLocal } from "./timing";

const PAGE_SIZE = 25;

const columns: Column<Schedule>[] = [
  {
    id: "state",
    header: "State",
    cell: (s) =>
      s.enabled ? <Badge tone="success">Enabled</Badge> : <Badge tone="neutral">Paused</Badge>,
  },
  {
    id: "name",
    header: "Schedule",
    cell: (s) => (
      <div className="min-w-[14rem]">
        <Link
          to={`/schedules/${s.id}`}
          className="font-medium text-ink hover:text-accent"
          onClick={(e) => e.stopPropagation()}
        >
          {s.name}
        </Link>
        <div className="mt-0.5 flex flex-wrap gap-x-3 text-xs text-muted">
          <span>{s.target_name ?? "Target deleted"}</span>
          {s.agent_profile_name ? <span>with {s.agent_profile_name}</span> : null}
          {s.is_demo ? <span>Demo data</span> : null}
        </div>
      </div>
    ),
  },
  {
    id: "when",
    header: "When",
    cell: (s) => (
      <div>
        <span className="text-ink-soft">{describeTiming(s)}</span>
        <span className="block text-xs text-muted">{s.timezone}</span>
      </div>
    ),
  },
  {
    id: "next",
    header: "Next run",
    cell: (s) =>
      !s.enabled ? (
        <span className="text-faint">Paused</span>
      ) : s.next_run_at ? (
        <div>
          <RelativeTime iso={s.next_run_at} className="text-ink-soft" />
          <span className="block text-xs text-muted">{formatLocal(s.next_run_local)}</span>
        </div>
      ) : (
        <span className="text-faint">No more runs</span>
      ),
  },
  {
    id: "last",
    header: "Last run",
    cell: (s) =>
      s.last_run_id && s.last_run_status ? (
        <div className="flex flex-col items-start gap-0.5">
          <StatusPill status={s.last_run_status} size="sm" />
          <RelativeTime iso={s.last_run_at} className="text-xs text-muted" />
        </div>
      ) : s.last_occurrence_status ? (
        <OccurrenceBadge status={s.last_occurrence_status} />
      ) : (
        <span className="text-faint">Never run</span>
      ),
  },
  {
    id: "policy",
    header: "If still running",
    cell: (s) => (
      <span className="text-ink-soft">{CONCURRENCY_COPY[s.concurrency_policy].label}</span>
    ),
  },
];

export function SchedulesPage() {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const offset = Number(params.get("offset") ?? 0) || 0;
  const q = params.get("q") ?? "";
  const [search, setSearch] = useState(q);
  const schedules = useSchedules({ q: q || undefined, offset, limit: PAGE_SIZE });

  useEffect(() => {
    const id = setTimeout(() => {
      if (search !== q) {
        setParams(
          (prev) => {
            const next = new URLSearchParams(prev);
            if (search) next.set("q", search);
            else next.delete("q");
            next.delete("offset");
            return next;
          },
          { replace: true },
        );
      }
    }, 300);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- debounce on the input only
  }, [search]);

  const newButton = (
    <Button
      variant="primary"
      icon={<Plus aria-hidden className="size-4" />}
      onClick={() => void navigate("/schedules/new")}
    >
      New schedule
    </Button>
  );

  return (
    <Page
      title="Schedules"
      description="Run a saved prompt at a set time, on an interval, or on a cron schedule in a time zone you choose. Each occurrence becomes a normal run you can watch in Runs."
      actions={
        <>
          <Link
            to="/settings/triggers"
            className="inline-flex h-9 items-center gap-2 rounded-[var(--radius-control)] px-3 text-ink-soft hover:bg-sunken hover:text-ink"
          >
            <Webhook aria-hidden className="size-4" />
            Webhook triggers
          </Link>
          {newButton}
        </>
      }
    >
      <SchedulerStatusNote />
      <label className="relative flex max-w-md items-center">
        <span className="sr-only">Search schedules</span>
        <Search aria-hidden className="absolute left-2.5 size-4 text-faint" />
        <input
          type="search"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search by name"
          className="h-9 w-full rounded-[var(--radius-control)] border border-line-strong bg-surface pr-2 pl-8 text-ink"
        />
      </label>
      {schedules.isPending ? (
        <LoadingBlock label="Loading schedules" />
      ) : schedules.isError ? (
        <ErrorState error={schedules.error} onRetry={() => void schedules.refetch()} />
      ) : schedules.data.total === 0 && !q ? (
        <EmptyState
          icon={<CalendarClock className="size-8" aria-hidden />}
          title="No schedules yet"
          description="Pick a saved prompt and an agent, choose when it should run, and preview the next run times before you save."
          action={newButton}
        />
      ) : (
        <DataTable
          caption="Schedules"
          columns={columns}
          rows={schedules.data.items}
          rowKey={(s) => s.id}
          busy={schedules.isFetching}
          onRowClick={(s) => void navigate(`/schedules/${s.id}`)}
          empty="No schedules match that search."
          pagination={{
            offset,
            limit: PAGE_SIZE,
            total: schedules.data.total,
            onChange: (next) =>
              setParams(
                (prev) => {
                  const p = new URLSearchParams(prev);
                  if (next) p.set("offset", String(next));
                  else p.delete("offset");
                  return p;
                },
                { replace: true },
              ),
          }}
        />
      )}
    </Page>
  );
}
