/**
 * The "Files" run-detail tab: the evidence completeness statement, summary counts,
 * protected-path and outside-boundary findings, and the filterable file ledger with a
 * per-file evidence drawer and sanitized exports.
 */
import { useQueryClient } from "@tanstack/react-query";
import { Download, FolderSearch, ShieldAlert, TriangleAlert } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router";

import { describeError, downloadFromApi } from "@/api/client";
import type { RunDetail } from "@/api/types";
import {
  Badge,
  Button,
  Card,
  DataTable,
  ErrorState,
  LoadingBlock,
  RelativeTime,
  Unavailable,
  useToast,
  type Column,
} from "@/components/ui";
import { humanize } from "@/lib/format";

import {
  EMPTY_FILTERS,
  filesKey,
  ledgerQuery,
  useRunFiles,
  useRunFileSummary,
  type FileCounts,
  type FileEvidenceSummary,
  type FileFinding,
  type LedgerFilters,
  type RunFile,
} from "./api";
import { FileDrawer } from "./FileDrawer";
import {
  GROUP_LABELS,
  classificationTone,
  evidenceSourceLabel,
  groupFiles,
  netStatusTone,
  policyWording,
  protectedLabel,
  type GroupBy,
} from "./helpers";

const PAGE_SIZE = 50;

const LEVEL_TONE = {
  complete: "success",
  partial: "warning",
  unavailable: "danger",
  pending: "live",
} as const;

const TILES: { key: keyof FileCounts; label: string }[] = [
  { key: "read_or_referenced", label: "Files read or referenced" },
  { key: "created", label: "Created" },
  { key: "modified", label: "Modified" },
  { key: "deleted", label: "Deleted" },
  { key: "renamed", label: "Renamed" },
  { key: "restored_or_net_unchanged", label: "Restored / net unchanged" },
  { key: "protected_findings", label: "Protected-path findings" },
  { key: "outside_findings", label: "Outside-boundary findings" },
];

const SORTS: { value: LedgerFilters["sort"]; label: string }[] = [
  { value: "path", label: "Path" },
  { value: "first_activity", label: "First activity" },
  { value: "last_activity", label: "Last activity" },
  { value: "operations", label: "Operation count" },
  { value: "diff_size", label: "Diff size" },
];

const CONTROL =
  "h-9 rounded-[var(--radius-control)] border border-line-strong bg-surface px-2 text-sm text-ink";

function Tile({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="rounded-[var(--radius-control)] border border-line bg-surface px-3 py-2">
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="mt-0.5 text-lg font-semibold tabular-nums">{children}</dd>
    </div>
  );
}

function SummaryPanel({ summary }: { summary: FileEvidenceSummary }) {
  const c = summary.counts;
  return (
    <Card padded>
      <div className="flex flex-col gap-3">
        <p className="flex flex-wrap items-center gap-2 text-sm">
          <Badge tone={LEVEL_TONE[summary.completeness.level]}>
            Evidence {summary.completeness.level}
          </Badge>
          <span className="text-ink-soft">{summary.completeness.statement}</span>
        </p>
        <dl className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
          {TILES.map((t) => {
            const value = c[t.key];
            return (
              <Tile key={t.key} label={t.label}>
                {value === null ? (
                  <Unavailable reason={summary.unavailable[t.key] ?? "Unavailable"} />
                ) : (
                  value
                )}
              </Tile>
            );
          })}
          <Tile label="Collapsed noisy activity">
            {c.collapsed_events}{" "}
            <span className="text-sm font-normal text-muted">
              events in {c.collapsed_directories} dir{c.collapsed_directories === 1 ? "" : "s"}
            </span>
          </Tile>
        </dl>
      </div>
    </Card>
  );
}

function FindingLine({ f }: { f: FileFinding }) {
  const approvalId = f.policy?.approval_id;
  const approved = f.policy?.decision === "approved";
  return (
    <li>
      <code>{f.file_key}</code> ({humanize(f.classification)}
      {f.patterns.length ? `, rule ${f.patterns.join(", ")}` : ""}):{" "}
      <span className="font-medium">{policyWording(f.policy)}</span>
      {approvalId ? (
        <>
          {" "}
          <Link
            to={approved ? "/approvals?view=resolved" : "/approvals"}
            className="text-accent underline"
          >
            view approval
          </Link>
        </>
      ) : null}
    </li>
  );
}

function Findings({ summary }: { summary: FileEvidenceSummary }) {
  return (
    <>
      {summary.protected.length ? (
        <div
          role="status"
          className="flex items-start gap-2 rounded-[var(--radius-control)] bg-[var(--st-cancelling-bg)] px-3 py-2 text-sm"
        >
          <ShieldAlert aria-hidden className="mt-0.5 size-4 text-[var(--st-cancelling)]" />
          <div className="min-w-0 text-ink-soft">
            <p className="font-medium text-ink">
              {summary.protected.length} protected-path finding
              {summary.protected.length === 1 ? "" : "s"}
            </p>
            <ul>
              {summary.protected.map((f) => (
                <FindingLine key={f.file_key} f={f} />
              ))}
            </ul>
          </div>
        </div>
      ) : null}
      {summary.outside.length ? (
        <div
          role="status"
          className="flex items-start gap-2 rounded-[var(--radius-control)] bg-[var(--st-failed-bg)] px-3 py-2 text-sm"
        >
          <TriangleAlert aria-hidden className="mt-0.5 size-4 text-[var(--st-failed)]" />
          <div className="min-w-0 text-ink-soft">
            <p className="font-medium text-ink">
              {summary.outside.length} outside-boundary finding
              {summary.outside.length === 1 ? "" : "s"}
            </p>
            <ul>
              {summary.outside.map((f) => (
                <FindingLine key={f.file_key} f={f} />
              ))}
            </ul>
          </div>
        </div>
      ) : null}
    </>
  );
}

function Select({
  label,
  value,
  onChange,
  options,
  any,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: { value: string; label: string }[];
  any?: string;
}) {
  return (
    <label className="flex flex-col gap-1 text-xs text-muted">
      {label}
      <select value={value} onChange={(e) => onChange(e.target.value)} className={CONTROL}>
        {any !== undefined ? <option value="">{any}</option> : null}
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </label>
  );
}

function Toggle({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label className="inline-flex items-center gap-1.5 text-sm">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="accent-[var(--accent)]"
      />
      {label}
    </label>
  );
}

function FilterBar({
  summary,
  filters,
  setFilters,
  groupBy,
  setGroupBy,
}: {
  summary: FileEvidenceSummary;
  filters: LedgerFilters;
  setFilters: (patch: Partial<LedgerFilters>) => void;
  groupBy: GroupBy;
  setGroupBy: (g: GroupBy) => void;
}) {
  const facet = (key: keyof FileEvidenceSummary["facets"], label: (v: string) => string) =>
    (summary.facets[key] ?? []).map((v) => ({ value: v, label: label(v) }));
  const [q, setQ] = useState(filters.q);
  // Debounce the search box so typing does not fire a request per keystroke.
  useEffect(() => {
    if (q === filters.q) return;
    const id = setTimeout(() => setFilters({ q }), 300);
    return () => clearTimeout(id);
  }, [q, filters.q, setFilters]);
  return (
    <div className="flex flex-col gap-3" role="search" aria-label="Filter the file ledger">
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex min-w-[14rem] flex-1 flex-col gap-1 text-xs text-muted">
          Search paths
          <input
            type="search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="e.g. src/ or greeter"
            className={CONTROL}
          />
        </label>
        <Select
          label="Classification"
          any="Any classification"
          value={filters.classification}
          onChange={(v) => setFilters({ classification: v })}
          options={facet("classification", humanize)}
        />
        <Select
          label="Operation"
          any="Any operation"
          value={filters.operation}
          onChange={(v) => setFilters({ operation: v })}
          options={facet("operation", humanize)}
        />
        <Select
          label="Net Git status"
          any="Any status"
          value={filters.net_status}
          onChange={(v) => setFilters({ net_status: v })}
          options={facet("net_status", humanize)}
        />
        <Select
          label="Evidence source"
          any="Any source"
          value={filters.source}
          onChange={(v) => setFilters({ source: v })}
          options={facet("source", evidenceSourceLabel)}
        />
        {(summary.facets.step ?? []).length ? (
          <Select
            label="Workflow step"
            any="Any step"
            value={filters.step}
            onChange={(v) => setFilters({ step: v })}
            options={facet("step", (v) => v)}
          />
        ) : null}
        <Select
          label="Agent"
          any="Any agent"
          value={filters.agent}
          onChange={(v) => setFilters({ agent: v })}
          options={facet("agent", (v) => v)}
        />
      </div>
      <div className="flex flex-wrap items-end gap-x-4 gap-y-3">
        <Toggle
          label="Protected paths"
          checked={filters.protected}
          onChange={(v) => setFilters({ protected: v })}
        />
        <Toggle
          label="Outside boundary"
          checked={filters.outside}
          onChange={(v) => setFilters({ outside: v })}
        />
        <Toggle
          label="Generated"
          checked={filters.generated}
          onChange={(v) => setFilters({ generated: v })}
        />
        <Toggle
          label="Net unchanged"
          checked={filters.net_unchanged}
          onChange={(v) => setFilters({ net_unchanged: v })}
        />
        <span className="ml-auto flex flex-wrap items-end gap-3">
          <Select
            label="Group by"
            value={groupBy}
            onChange={(v) => setGroupBy(v as GroupBy)}
            options={Object.entries(GROUP_LABELS).map(([value, label]) => ({ value, label }))}
          />
          <Select
            label="Sort by"
            value={filters.sort}
            onChange={(v) => setFilters({ sort: v as LedgerFilters["sort"] })}
            options={SORTS}
          />
          <Select
            label="Order"
            value={filters.order}
            onChange={(v) => setFilters({ order: v as LedgerFilters["order"] })}
            options={[
              { value: "asc", label: "Ascending" },
              { value: "desc", label: "Descending" },
            ]}
          />
          <Button
            size="md"
            variant="ghost"
            onClick={() => {
              setQ("");
              setFilters(EMPTY_FILTERS);
              setGroupBy("none");
            }}
          >
            Reset
          </Button>
        </span>
      </div>
    </div>
  );
}

function columns(onOpen: (path: string) => void, stepUnavailable: boolean): Column<RunFile>[] {
  return [
    {
      id: "class",
      header: "Classification",
      cell: (f) => (
        <span className="flex flex-col items-start gap-1">
          <Badge tone={classificationTone(f)}>{f.label}</Badge>
          {f.net_unchanged ? <Badge tone="warning">Touched, net unchanged</Badge> : null}
        </span>
      ),
    },
    {
      id: "path",
      header: "Path",
      cell: (f) =>
        f.is_collapsed ? (
          <span className="text-sm">
            <code>{f.file_key}</code>{" "}
            <span className="text-muted">
              ({(f.collapsed_count ?? 0).toLocaleString()} filesystem events collapsed by policy)
            </span>
          </span>
        ) : (
          <span className="min-w-0">
            <button
              type="button"
              onClick={() => onOpen(f.file_key)}
              className="text-left font-mono text-sm break-all text-accent hover:underline"
            >
              {f.file_key}
            </button>
            {f.rename_chain.length ? (
              <span className="block text-xs text-muted">from {f.rename_chain.join(" → ")}</span>
            ) : f.start_path && f.start_path !== f.file_key ? (
              <span className="block text-xs text-muted">from {f.start_path}</span>
            ) : null}
          </span>
        ),
    },
    {
      id: "net",
      header: "Net Git status",
      cell: (f) => <Badge tone={netStatusTone(f.net_status)}>{humanize(f.net_status)}</Badge>,
    },
    {
      id: "when",
      header: "First / last activity",
      cell: (f) => (
        <span className="flex flex-col text-xs whitespace-nowrap">
          <RelativeTime iso={f.first_at} />
          <RelativeTime iso={f.last_at} className="text-muted" />
        </span>
      ),
    },
    { id: "ops", header: "Operations", align: "right", cell: (f) => f.operation_total },
    {
      id: "step",
      header: "Step / agent",
      cell: (f) => (
        <span className="flex flex-col text-xs">
          {f.first_step ? (
            <span>{f.first_step}</span>
          ) : stepUnavailable ? (
            <Unavailable reason="Step attribution unavailable" className="text-xs" />
          ) : null}
          <span className="text-muted">{f.agents.join(", ")}</span>
        </span>
      ),
    },
    {
      id: "sources",
      header: "Evidence",
      cell: (f) => (
        <span className="flex flex-wrap gap-1">
          {f.sources.map((s) => (
            <Badge key={s}>{evidenceSourceLabel(s)}</Badge>
          ))}
        </span>
      ),
    },
    {
      id: "policy",
      header: "Boundary",
      cell: (f) => (
        <span className="flex flex-col items-start gap-1">
          {f.is_protected ? (
            <Badge tone={f.policy?.decision === "approved" ? "success" : "warning"}>
              {protectedLabel(f)}
            </Badge>
          ) : null}
          {f.is_outside ? <Badge tone="danger">Outside boundary</Badge> : null}
          {f.is_generated ? <Badge>Generated</Badge> : null}
          {!f.is_protected && !f.is_outside && !f.is_generated ? (
            <span className="text-faint">Inside boundary</span>
          ) : null}
        </span>
      ),
    },
    {
      id: "diff",
      header: "Diff",
      align: "right",
      cell: (f) =>
        f.is_binary ? (
          <span className="text-muted">binary</span>
        ) : f.insertions === null || f.deletions === null ? (
          <span className="text-muted italic">unknown</span>
        ) : (
          <span className="whitespace-nowrap">
            <span className="text-[var(--st-succeeded)]">+{f.insertions}</span>{" "}
            <span className="text-[var(--st-failed)]">−{f.deletions}</span>
          </span>
        ),
    },
    {
      id: "completeness",
      header: "Completeness",
      cell: (f) => <span className="text-xs">{humanize(f.completeness)}</span>,
    },
  ];
}

function Exports({ runId, filters }: { runId: string; filters: LedgerFilters }) {
  const toast = useToast();
  const [busy, setBusy] = useState<string | null>(null);
  const download = (id: string, path: string, filename: string) => {
    setBusy(id);
    downloadFromApi(path, filename)
      .catch((e: unknown) => toast.show(describeError(e), "error"))
      .finally(() => setBusy(null));
  };
  const ledger = (format: "csv" | "json") => {
    const qs = new URLSearchParams({ ...ledgerQuery(filters), format }).toString();
    download(format, `/runs/${runId}/files/export?${qs}`, `agenttape-run-${runId}-files.${format}`);
  };
  const icon = <Download className="size-4" aria-hidden />;
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Button size="sm" icon={icon} loading={busy === "csv"} onClick={() => ledger("csv")}>
        Export ledger (CSV)
      </Button>
      <Button size="sm" icon={icon} loading={busy === "json"} onClick={() => ledger("json")}>
        Export ledger (JSON)
      </Button>
      <Button
        size="sm"
        icon={icon}
        loading={busy === "bundle"}
        onClick={() =>
          download(
            "bundle",
            `/runs/${runId}/evidence/export?format=json`,
            `agenttape-run-${runId}-evidence.json`,
          )
        }
      >
        Export evidence bundle
      </Button>
    </div>
  );
}

export function FilesTab({ detail, live }: { detail: RunDetail; live: boolean }) {
  const runId = detail.run.id;
  const client = useQueryClient();
  const [filters, setFiltersState] = useState<LedgerFilters>(EMPTY_FILTERS);
  const [groupBy, setGroupBy] = useState<GroupBy>("none");
  const [offset, setOffset] = useState(0);
  // The open drawer's path lives in the URL (`?path=`) so a citation's header link, or a
  // pasted URL, opens the file drawer directly; closing it clears the param.
  const [params, setParams] = useSearchParams();
  const open = params.get("path");
  const setOpen = useCallback(
    (path: string | null) => {
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          if (path === null) next.delete("path");
          else next.set("path", path);
          return next;
        },
        { replace: true },
      );
    },
    [setParams],
  );
  const summary = useRunFileSummary(runId, live);
  const files = useRunFiles(runId, filters, { limit: PAGE_SIZE, offset }, live);

  // When the run stops being live, fetch once more to pick up the final reconciliation.
  const wasLive = useRef(live);
  useEffect(() => {
    if (wasLive.current && !live) void client.invalidateQueries({ queryKey: filesKey(runId) });
    wasLive.current = live;
  }, [live, runId, client]);

  const setFilters = useCallback((patch: Partial<LedgerFilters>) => {
    setFiltersState((f) => ({ ...f, ...patch }));
    setOffset(0);
  }, []);

  if (summary.isPending) return <LoadingBlock label="Loading file evidence" />;
  if (summary.isError)
    return <ErrorState error={summary.error} onRetry={() => void summary.refetch()} />;
  const s = summary.data;
  const stepUnavailable = s.completeness.step_attribution === "unavailable";
  const cols = columns(setOpen, stepUnavailable);
  const page = files.data;
  const groups = page ? groupFiles(page.items, groupBy, evidenceSourceLabel) : [];

  return (
    <div className="flex flex-col gap-4">
      <SummaryPanel summary={s} />
      <Findings summary={s} />
      <Card
        title="File ledger"
        description="One row per logical file, from agent reports, the filesystem watcher, command arguments, and Git."
        actions={
          detail.run.project_id ? (
            <Link
              to={`/files/${detail.run.project_id}`}
              className="inline-flex items-center gap-1.5 text-sm text-accent hover:underline"
            >
              <FolderSearch aria-hidden className="size-4" />
              Project file activity
            </Link>
          ) : null
        }
      >
        <div className="flex flex-col gap-4">
          <FilterBar
            summary={s}
            filters={filters}
            setFilters={setFilters}
            groupBy={groupBy}
            setGroupBy={setGroupBy}
          />
          <Exports runId={runId} filters={filters} />
          {files.isError ? (
            <ErrorState error={files.error} onRetry={() => void files.refetch()} />
          ) : !page ? (
            <LoadingBlock label="Loading the file ledger" />
          ) : (
            <>
              <p className="text-sm text-muted" aria-live="polite">
                {page.total} {page.total === 1 ? "entry" : "entries"}
                {live ? " so far; updating live" : ""}
              </p>
              {groups.map((g, i) => (
                <section
                  key={g.key}
                  className="flex flex-col gap-2"
                  aria-label={g.label || undefined}
                >
                  {g.label ? <h3 className="text-sm font-semibold">{g.label}</h3> : null}
                  <DataTable
                    caption={g.label ? `File ledger: ${g.label}` : "File ledger"}
                    columns={cols}
                    rows={g.rows}
                    rowKey={(f) => f.id}
                    busy={files.isFetching}
                    pagination={
                      i === groups.length - 1 && page.total > PAGE_SIZE
                        ? { offset, limit: PAGE_SIZE, total: page.total, onChange: setOffset }
                        : undefined
                    }
                    empty={
                      live
                        ? "No file evidence yet. Files appear here as the agent works."
                        : "No files match these filters."
                    }
                  />
                </section>
              ))}
            </>
          )}
        </div>
      </Card>
      <FileDrawer
        runId={runId}
        path={open}
        live={live}
        stepUnavailable={stepUnavailable}
        onClose={() => setOpen(null)}
      />
    </div>
  );
}
