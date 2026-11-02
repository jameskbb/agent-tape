/**
 * The Findings inbox: every finding recorded across every run in a window, flagged
 * verdicts first by default, the sidebar's entry point for interactive Claude Code sessions
 * nobody opens a run page for. Fed by the same cross-run read model the Overview's
 * "Verified findings" tile tallies.
 */
import { Search } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";

import { Page } from "@/components/layout/Page";
import {
  DataTable,
  EmptyState,
  ErrorState,
  LoadingBlock,
  RelativeTime,
  Select,
  StatusPill,
  WindowSwitch,
  controlClass,
  type Column,
} from "@/components/ui";
import { useProfiles } from "@/features/agents/api";
import { useProjects } from "@/features/projects/api";
import { cn } from "@/lib/cn";
import { humanize } from "@/lib/format";

import { useFindingsInbox, type FindingRow, type FindingsInboxParams } from "./api";
import { FINDINGS_DOC_URL } from "./capability";
import { badgeStatus, CHIPS, tallyFromCounts, tallyPhrases } from "./model";
import { VerdictBadge } from "./VerdictBadge";

const PAGE_SIZE = 50;
const KINDS = ["claim", "recommendation", "decision", "review"];
const SOURCES = [
  { value: "launched", label: "Launched run" },
  { value: "observed", label: "Observed session" },
];
const enc = encodeURIComponent;

/** "Launched run" for an agent/workflow run, "Observed session" for a hook-recorded one:
 * the run's `kind`, never its creation `source` (ui/cli/schedule/...). */
function runSourceLabel(kind: string): string {
  return kind === "observed" ? "Observed session" : "Launched run";
}

/** "{n} citations, {flagged phrase}" (e.g. "2 citations, 1 stale"), "1 citation" with
 * nothing flagged, or "No citation". The flagged phrase reuses `tallyPhrases` over the
 * row's own non-`quote_matches` verdict counts, so the wording never drifts from the tab's
 * own tally sentence. */
function citationsCell(row: FindingRow): string {
  if (row.citations === 0) return "No citation";
  const flagged = tallyPhrases(
    tallyFromCounts({ ...row.verdict_counts, quote_matches: 0, declared_only: 0 }),
  );
  const base = row.citations === 1 ? "1 citation" : `${row.citations} citations`;
  return flagged ? `${base}, ${flagged}` : base;
}

function findingHref(row: FindingRow): string {
  return `/runs/${enc(row.run.id)}?tab=findings&finding=${enc(row.key)}`;
}

const columns: Column<FindingRow>[] = [
  {
    id: "verdict",
    header: "Verdict",
    cell: (r) => <VerdictBadge verdict={badgeStatus(r.worst_verdict, false)} compact />,
  },
  {
    id: "claim",
    header: "Claim",
    cell: (r) => (
      <Link
        to={findingHref(r)}
        onClick={(e) => e.stopPropagation()}
        className="line-clamp-2 block max-w-[36rem] font-medium break-words text-ink hover:text-accent"
      >
        {r.text ?? humanize(r.kind ?? "finding")}
      </Link>
    ),
  },
  { id: "citations", header: "Citations", cell: citationsCell },
  {
    id: "run",
    header: "Run",
    cell: (r) => (
      <span className="flex flex-wrap items-center gap-2">
        <Link
          to={`/runs/${enc(r.run.id)}`}
          onClick={(e) => e.stopPropagation()}
          className="text-accent hover:underline"
        >
          {r.run.title}
        </Link>
        <StatusPill status={r.run.status} size="sm" />
      </span>
    ),
  },
  { id: "agent", header: "Agent", cell: (r) => r.run.agent_label },
  { id: "source", header: "Source", cell: (r) => runSourceLabel(r.run.kind) },
  {
    id: "when",
    header: "When",
    sortKey: "when",
    cell: (r) => <RelativeTime iso={r.created_at} />,
  },
];

/** Every filter and page value lives in the URL, so a link to the inbox reproduces exactly
 * what its sender saw. `update` resets `offset` on every change except an explicit one
 * (the pager itself passes `resetOffset=false`). */
function useInboxFilters() {
  const [params, setParams] = useSearchParams();
  const filters: FindingsInboxParams & { days: number } = {
    days: Number(params.get("days") ?? 14) || 14,
    project_id: params.get("project_id") ?? undefined,
    agent_profile_id: params.get("agent_profile_id") ?? undefined,
    verdict: params.get("verdict") ?? undefined,
    kind: params.get("kind") ?? undefined,
    source: params.get("source") ?? undefined,
    q: params.get("q") ?? undefined,
    sort: params.get("sort") === "newest" ? "newest" : "flagged",
    offset: Number(params.get("offset") ?? 0) || 0,
    limit: PAGE_SIZE,
  };
  const update = (patch: Record<string, string | null>, resetOffset = true) => {
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        for (const [key, value] of Object.entries(patch)) {
          if (value === null || value === "") next.delete(key);
          else next.set(key, value);
        }
        if (resetOffset && !("offset" in patch)) next.delete("offset");
        return next;
      },
      { replace: true },
    );
  };
  return { filters, update };
}

export function FindingsInboxPage() {
  const { filters, update } = useInboxFilters();
  const navigate = useNavigate();
  const query = useFindingsInbox(filters);
  const projects = useProjects({ limit: 100 });
  const profiles = useProfiles({ limit: 100 });
  const [search, setSearch] = useState(filters.q ?? "");

  useEffect(() => {
    const id = setTimeout(() => {
      if ((filters.q ?? "") !== search) update({ q: search || null });
    }, 300);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- debounce on the input only
  }, [search]);

  const anyFilter = Boolean(
    filters.project_id ||
    filters.agent_profile_id ||
    filters.verdict ||
    filters.kind ||
    filters.source ||
    filters.q,
  );

  const clearFilters = () => {
    setSearch("");
    update({
      project_id: null,
      agent_profile_id: null,
      verdict: null,
      kind: null,
      source: null,
      q: null,
    });
  };

  return (
    <Page
      title="Findings"
      description="Every finding recorded across runs in the window, flagged verdicts first."
      actions={<WindowSwitch value={filters.days} onChange={(d) => update({ days: String(d) })} />}
    >
      <form
        role="search"
        aria-label="Filter findings"
        className="flex flex-wrap items-center gap-3"
      >
        <label className="relative flex min-w-[14rem] flex-1 items-center">
          <span className="sr-only">Search claims</span>
          <Search aria-hidden className="absolute left-2.5 size-4 text-faint" />
          <input
            type="search"
            aria-label="Search claims"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search claims"
            className={cn(controlClass, "h-9 w-full pr-2 pl-8")}
          />
        </label>
        <Select
          label="Project"
          any="Any"
          value={filters.project_id ?? ""}
          onChange={(v) => update({ project_id: v || null })}
          options={(projects.data?.items ?? []).map((p) => ({ value: p.id, label: p.name }))}
        />
        <Select
          label="Agent"
          any="Any"
          value={filters.agent_profile_id ?? ""}
          onChange={(v) => update({ agent_profile_id: v || null })}
          options={(profiles.data?.items ?? []).map((p) => ({ value: p.id, label: p.name }))}
        />
        <Select
          label="Verdict"
          any="Any"
          value={filters.verdict ?? ""}
          onChange={(v) => update({ verdict: v || null })}
          options={CHIPS.map((c) => ({ value: c.id, label: c.label }))}
        />
        <Select
          label="Kind"
          any="Any"
          value={filters.kind ?? ""}
          onChange={(v) => update({ kind: v || null })}
          options={KINDS.map((k) => ({ value: k, label: humanize(k) }))}
        />
        <Select
          label="Source"
          any="Any"
          value={filters.source ?? ""}
          onChange={(v) => update({ source: v || null })}
          options={SOURCES}
        />
      </form>

      {query.isPending ? (
        <LoadingBlock label="Loading findings" />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : query.data.total === 0 ? (
        anyFilter ? (
          <EmptyState
            title="No findings match these filters."
            action={
              <button
                type="button"
                onClick={clearFilters}
                className="text-sm text-accent hover:underline"
              >
                Clear filters
              </button>
            }
          />
        ) : (
          <EmptyState
            title="No findings in this window."
            description="Findings are recorded by Claude Code through the AgentTape MCP server; launched runs record them automatically."
            action={
              <a
                href={FINDINGS_DOC_URL}
                target="_blank"
                rel="noreferrer"
                className="text-sm text-accent hover:underline"
              >
                How findings are recorded
              </a>
            }
          />
        )
      ) : (
        <DataTable
          caption="Findings across runs"
          columns={columns}
          rows={query.data.items}
          rowKey={(r) => r.id}
          busy={query.isFetching}
          sort={{ key: "when", order: filters.sort === "newest" ? "desc" : "asc" }}
          onSort={() => update({ sort: filters.sort === "newest" ? null : "newest" })}
          onRowClick={(r) => void navigate(findingHref(r))}
          pagination={{
            offset: filters.offset,
            limit: PAGE_SIZE,
            total: query.data.total,
            onChange: (next) => update({ offset: next ? String(next) : null }, false),
          }}
        />
      )}
    </Page>
  );
}
