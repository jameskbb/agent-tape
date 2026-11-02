import { Search } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";

import { useRuns, type RunListParams } from "@/api/hooks";
import { RUN_STATUSES, type Run, type RunStatus } from "@/api/types";
import { StartDemoRun } from "@/components/demo/StartDemoRun";
import { Page } from "@/components/layout/Page";
import {
  Badge,
  DataTable,
  Duration,
  EmptyState,
  ErrorState,
  LoadingBlock,
  RelativeTime,
  StatusPill,
  statusLabel,
  type Column,
} from "@/components/ui";
import { cn } from "@/lib/cn";
import { humanize, sourceLabel } from "@/lib/format";

const PAGE_SIZE = 25;
const SOURCES = ["ui", "api", "cli", "schedule", "webhook", "workflow", "replay", "fork", "demo"];

function useRunFilters() {
  const [params, setParams] = useSearchParams();
  const status = params
    .getAll("status")
    .filter((s): s is RunStatus => (RUN_STATUSES as string[]).includes(s));
  const filters: RunListParams = {
    status,
    source: params.getAll("source"),
    q: params.get("q") ?? undefined,
    sort: params.get("sort") ?? "created_at",
    order: params.get("order") === "asc" ? "asc" : "desc",
    offset: Number(params.get("offset") ?? 0) || 0,
    limit: PAGE_SIZE,
  };
  const update = (patch: Record<string, string | string[] | null>, resetOffset = true) => {
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        for (const [key, value] of Object.entries(patch)) {
          next.delete(key);
          if (Array.isArray(value)) value.forEach((v) => next.append(key, v));
          else if (value !== null && value !== "") next.set(key, value);
        }
        if (resetOffset && !("offset" in patch)) next.delete("offset");
        return next;
      },
      { replace: true },
    );
  };
  return { filters, update };
}

const columns: Column<Run>[] = [
  {
    id: "status",
    header: "Status",
    sortKey: "status",
    cell: (r) => <StatusPill status={r.status} size="sm" />,
  },
  {
    id: "title",
    header: "Run",
    sortKey: "title",
    cell: (r) => (
      <div className="min-w-[16rem]">
        <Link
          to={`/runs/${r.id}`}
          className="font-medium text-ink hover:text-accent"
          onClick={(e) => e.stopPropagation()}
        >
          {r.title}
        </Link>
        <div className="mt-0.5 flex flex-wrap gap-x-3 text-xs text-muted">
          <code>{r.id.slice(0, 8)}</code>
          {r.failure_category ? (
            <span className="text-[var(--st-failed)]">{humanize(r.failure_category)}</span>
          ) : null}
          {r.queue_position ? <span>Position {r.queue_position} in queue</span> : null}
          {r.is_demo ? <span>Demo data</span> : null}
        </div>
      </div>
    ),
  },
  { id: "source", header: "Source", cell: (r) => <Badge>{sourceLabel(r.source)}</Badge> },
  {
    id: "adapter",
    header: "Agent",
    cell: (r) => (
      <span className="text-ink-soft">
        {r.agent_profile_name ?? r.adapter_type ?? humanize(r.kind)}
      </span>
    ),
  },
  {
    id: "created",
    header: "Created",
    sortKey: "created_at",
    cell: (r) => <RelativeTime iso={r.created_at} className="text-ink-soft" />,
  },
  {
    id: "duration",
    header: "Duration",
    sortKey: "duration_ms",
    align: "right",
    cell: (r) =>
      r.duration_ms !== null ? (
        <Duration ms={r.duration_ms} />
      ) : r.started_at && !r.finished_at ? (
        <Duration start={r.started_at} live />
      ) : (
        <span className="text-faint">Not started</span>
      ),
  },
  {
    id: "attempts",
    header: "Attempts",
    align: "right",
    cell: (r) => `${r.attempt_count}/${r.max_attempts}`,
  },
];

export function RunsPage() {
  const { filters, update } = useRunFilters();
  const navigate = useNavigate();
  const runs = useRuns(filters);
  const [search, setSearch] = useState(filters.q ?? "");

  useEffect(() => {
    const id = setTimeout(() => {
      if ((filters.q ?? "") !== search) update({ q: search || null });
    }, 300);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- debounce on the input only
  }, [search]);

  const toggleStatus = (s: RunStatus) => {
    const current = filters.status ?? [];
    update({ status: current.includes(s) ? current.filter((x) => x !== s) : [...current, s] });
  };
  const anyFilter = Boolean(
    (filters.status?.length ?? 0) || (filters.source?.length ?? 0) || filters.q,
  );

  return (
    <Page
      title="Runs"
      description="Every agent run, whatever started it: the UI, the CLI, the API, a schedule, a webhook, or a workflow."
      actions={<StartDemoRun />}
    >
      <div className="flex flex-col gap-3" role="search" aria-label="Filter runs">
        <div className="flex flex-wrap items-center gap-2">
          <label className="relative flex min-w-[16rem] flex-1 items-center">
            <span className="sr-only">Search runs</span>
            <Search aria-hidden className="absolute left-2.5 size-4 text-faint" />
            <input
              type="search"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search by title or run id"
              className="h-9 w-full rounded-[var(--radius-control)] border border-line-strong bg-surface pr-2 pl-8 text-ink"
            />
          </label>
          <label className="flex items-center gap-2 text-sm text-muted">
            Source
            <select
              value={filters.source?.[0] ?? ""}
              onChange={(e) => update({ source: e.target.value ? [e.target.value] : [] })}
              className="h-9 rounded-[var(--radius-control)] border border-line-strong bg-surface px-2 text-ink"
            >
              <option value="">Any source</option>
              {SOURCES.map((s) => (
                <option key={s} value={s}>
                  {sourceLabel(s)}
                </option>
              ))}
            </select>
          </label>
        </div>
        <div className="flex flex-wrap gap-1.5" role="group" aria-label="Status">
          {RUN_STATUSES.map((s) => {
            const on = filters.status?.includes(s) ?? false;
            return (
              <button
                key={s}
                type="button"
                aria-pressed={on}
                onClick={() => toggleStatus(s)}
                className={cn(
                  "rounded-full border px-2.5 py-0.5 text-sm transition-colors",
                  on
                    ? "border-accent bg-accent-soft text-ink"
                    : "border-line bg-surface text-ink-soft hover:border-line-strong",
                )}
              >
                {statusLabel(s)}
              </button>
            );
          })}
          {anyFilter ? (
            <button
              type="button"
              onClick={() => {
                setSearch("");
                update({ status: [], source: [], q: null });
              }}
              className="px-2 text-sm text-accent hover:underline"
            >
              Clear filters
            </button>
          ) : null}
        </div>
      </div>

      {runs.isPending ? (
        <LoadingBlock label="Loading runs" />
      ) : runs.isError ? (
        <ErrorState error={runs.error} onRetry={() => void runs.refetch()} />
      ) : runs.data.total === 0 && !anyFilter ? (
        <EmptyState
          title="No runs yet"
          description="Start a Demo Agent run to see the whole flight recorder at work: live events, commands, a real diff, artifacts, and evaluations. No credentials needed."
          action={<StartDemoRun />}
        />
      ) : (
        <DataTable
          caption="Runs"
          columns={columns}
          rows={runs.data.items}
          rowKey={(r) => r.id}
          busy={runs.isFetching}
          sort={{ key: filters.sort ?? "created_at", order: filters.order ?? "desc" }}
          onSort={(s) => update({ sort: s.key, order: s.order })}
          onRowClick={(r) => void navigate(`/runs/${r.id}`)}
          empty="No runs match these filters."
          pagination={{
            offset: filters.offset ?? 0,
            limit: PAGE_SIZE,
            total: runs.data.total,
            onChange: (offset) => update({ offset: offset ? String(offset) : null }, false),
          }}
        />
      )}
    </Page>
  );
}
