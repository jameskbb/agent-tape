/**
 * Project File Activity: which files AgentTape runs touch most, which appear in failed runs,
 * which change together, and protected-path activity, each linked to its runs. Clicking a
 * path shows that file's run history.
 */
import { FolderSearch } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";

import type { RunStatus } from "@/api/types";
import {
  Badge,
  Card,
  DataTable,
  EmptyState,
  ErrorState,
  LoadingBlock,
  RelativeTime,
  StatusPill,
  type Column,
} from "@/components/ui";
import { Page } from "@/components/layout/Page";
import { useProjects } from "@/features/projects/api";
import { humanize, shortId } from "@/lib/format";

import {
  useFileHistory,
  useProjectFileActivity,
  type ActivityEntry,
  type FileRunHistoryItem,
  type RunFileRef,
} from "./api";
import { netStatusTone, policyWording } from "./helpers";

const HISTORY_PAGE = 20;

function PathButton({ path, onPick }: { path: string; onPick: (path: string) => void }) {
  return (
    <button
      type="button"
      onClick={() => onPick(path)}
      className="text-left font-mono text-sm break-all text-accent hover:underline"
    >
      {path}
    </button>
  );
}

function RunLinks({ ids }: { ids: string[] }) {
  return (
    <span className="flex flex-wrap gap-x-2 gap-y-0.5 text-xs">
      {ids.slice(0, 6).map((id) => (
        <Link
          key={id}
          to={`/runs/${id}?tab=files`}
          className="font-mono text-accent hover:underline"
        >
          {shortId(id)}
        </Link>
      ))}
      {ids.length > 6 ? <span className="text-muted">and {ids.length - 6} more</span> : null}
    </span>
  );
}

function entryColumns(onPick: (p: string) => void): Column<ActivityEntry>[] {
  return [
    { id: "path", header: "File", cell: (e) => <PathButton path={e.path} onPick={onPick} /> },
    { id: "runs", header: "Runs", align: "right", cell: (e) => e.runs },
    { id: "ops", header: "Operations", align: "right", cell: (e) => e.operations },
    { id: "last", header: "Last activity", cell: (e) => <RelativeTime iso={e.last_at} /> },
    { id: "ids", header: "Supporting runs", cell: (e) => <RunLinks ids={e.run_ids} /> },
  ];
}

function refColumns(onPick: (p: string) => void, withPolicy: boolean): Column<RunFileRef>[] {
  const cols: Column<RunFileRef>[] = [
    { id: "path", header: "File", cell: (r) => <PathButton path={r.path} onPick={onPick} /> },
    {
      id: "run",
      header: "Run",
      cell: (r) => (
        <span className="flex flex-wrap items-center gap-2">
          <Link to={`/runs/${r.run_id}?tab=files`} className="text-accent hover:underline">
            {r.run_title}
          </Link>
          <StatusPill status={r.run_status as RunStatus} />
        </span>
      ),
    },
    { id: "class", header: "Classification", cell: (r) => humanize(r.classification) },
    {
      id: "net",
      header: "Net Git status",
      cell: (r) => <Badge tone={netStatusTone(r.net_status)}>{humanize(r.net_status)}</Badge>,
    },
  ];
  if (withPolicy)
    cols.push({
      id: "policy",
      header: "Policy",
      cell: (r) => (
        <Badge tone={r.policy?.decision === "approved" ? "success" : "warning"}>
          {policyWording(r.policy)}
        </Badge>
      ),
    });
  cols.push({ id: "last", header: "Last activity", cell: (r) => <RelativeTime iso={r.last_at} /> });
  return cols;
}

const historyColumns: Column<FileRunHistoryItem>[] = [
  {
    id: "run",
    header: "Run",
    cell: (h) => (
      <Link to={`/runs/${h.run_id}?tab=files`} className="text-accent hover:underline">
        {h.run_title}
      </Link>
    ),
  },
  {
    id: "status",
    header: "Status",
    cell: (h) => <StatusPill status={h.run_status as RunStatus} />,
  },
  {
    id: "class",
    header: "Classification",
    cell: (h) => (
      <span className="flex flex-wrap gap-1">
        {humanize(h.classification)}
        {h.net_unchanged ? <Badge tone="warning">Touched, net unchanged</Badge> : null}
        {h.is_protected ? <Badge tone="warning">Protected</Badge> : null}
      </span>
    ),
  },
  {
    id: "net",
    header: "Net Git status",
    cell: (h) => <Badge tone={netStatusTone(h.net_status)}>{humanize(h.net_status)}</Badge>,
  },
  { id: "ops", header: "Operations", align: "right", cell: (h) => h.operation_total },
  {
    id: "diff",
    header: "Diff",
    align: "right",
    cell: (h) =>
      h.insertions === null || h.deletions === null ? (
        <span className="text-muted italic">unknown</span>
      ) : (
        <span className="whitespace-nowrap">
          <span className="text-[var(--st-succeeded)]">+{h.insertions}</span>{" "}
          <span className="text-[var(--st-failed)]">−{h.deletions}</span>
        </span>
      ),
  },
  { id: "last", header: "Last activity", cell: (h) => <RelativeTime iso={h.last_at} /> },
];

function History({
  projectId,
  path,
  onClose,
}: {
  projectId: string;
  path: string;
  onClose: () => void;
}) {
  const [offset, setOffset] = useState(0);
  const query = useFileHistory(projectId, path, offset);
  return (
    <Card
      title={
        <>
          Run history of <code>{path}</code>
        </>
      }
      description="Every AgentTape run in this project that touched this file."
      actions={
        <button type="button" onClick={onClose} className="text-sm text-accent hover:underline">
          Close history
        </button>
      }
    >
      {query.isPending ? (
        <LoadingBlock label="Loading file history" />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : (
        <DataTable
          caption={`Run history of ${path}`}
          columns={historyColumns}
          rows={query.data.items}
          rowKey={(h) => h.run_id}
          busy={query.isFetching}
          pagination={{
            offset,
            limit: HISTORY_PAGE,
            total: query.data.total,
            onChange: setOffset,
          }}
        />
      )}
    </Card>
  );
}

function ProjectPicker({ value }: { value?: string }) {
  const projects = useProjects({ limit: 100 });
  const navigate = useNavigate();
  return (
    <label className="flex items-center gap-2 text-sm text-muted">
      Project
      <select
        value={value ?? ""}
        onChange={(e) => {
          if (e.target.value) void navigate(`/files/${e.target.value}`);
        }}
        className="h-9 rounded-[var(--radius-control)] border border-line-strong bg-surface px-2 text-ink"
      >
        <option value="">Choose a project</option>
        {(projects.data?.items ?? []).map((p) => (
          <option key={p.id} value={p.id}>
            {p.name}
          </option>
        ))}
      </select>
    </label>
  );
}

const DESCRIPTION =
  "Which files agent runs touch, across every run of a project. Evidence comes from each run's file ledger.";

export function FileActivityIndexPage() {
  const projects = useProjects({ limit: 100 });
  return (
    <Page title="File activity" description={DESCRIPTION}>
      {projects.isPending ? (
        <LoadingBlock label="Loading projects" />
      ) : projects.isError ? (
        <ErrorState error={projects.error} onRetry={() => void projects.refetch()} />
      ) : projects.data.items.length === 0 ? (
        <EmptyState
          title="No projects yet"
          description="Add a project; file activity appears once its runs record file evidence."
          action={
            <Link to="/projects/new" className="text-accent hover:underline">
              Add a project
            </Link>
          }
        />
      ) : (
        <Card title="Choose a project">
          <ul className="flex flex-col divide-y divide-line">
            {projects.data.items.map((p) => (
              <li key={p.id} className="flex flex-wrap items-center gap-3 py-2">
                <FolderSearch aria-hidden className="size-4 text-muted" />
                <Link to={`/files/${p.id}`} className="font-medium text-accent hover:underline">
                  {p.name}
                </Link>
                {p.description ? (
                  <span className="min-w-0 flex-1 truncate text-sm text-muted">
                    {p.description}
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        </Card>
      )}
    </Page>
  );
}

export function FileActivityPage() {
  const { projectId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const path = params.get("path");
  const query = useProjectFileActivity(projectId);
  const pick = (p: string | null) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (p) next.set("path", p);
        else next.delete("path");
        return next;
      },
      { replace: true },
    );
  return (
    <Page
      title="File activity"
      breadcrumb={
        <Link to="/files" className="hover:text-ink">
          File activity
        </Link>
      }
      description={DESCRIPTION}
      actions={<ProjectPicker value={projectId} />}
    >
      {path ? (
        <History key={path} projectId={projectId} path={path} onClose={() => pick(null)} />
      ) : null}
      {query.isPending ? (
        <LoadingBlock label="Loading file activity" />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : (
        <>
          <p className="text-sm text-muted">
            {query.data.runs_with_evidence} of {query.data.runs_scanned} scanned run
            {query.data.runs_scanned === 1 ? "" : "s"} recorded file evidence.
          </p>
          <Card title="Most touched files" padded={false}>
            <DataTable
              caption="Most touched files"
              columns={entryColumns(pick)}
              rows={query.data.most_touched}
              rowKey={(e) => e.path}
              empty="No file evidence recorded yet."
            />
          </Card>
          <Card title="Files in failed runs" padded={false}>
            <DataTable
              caption="Files in failed runs"
              columns={entryColumns(pick)}
              rows={query.data.failed_run_files}
              rowKey={(e) => e.path}
              empty="No failed run touched a file."
            />
          </Card>
          <Card
            title="Changed together"
            description="Files changed in the same runs."
            padded={false}
          >
            <DataTable
              caption="Files changed together"
              columns={[
                {
                  id: "paths",
                  header: "Files",
                  cell: (c) => (
                    <span className="flex flex-col gap-0.5">
                      {c.paths.map((p) => (
                        <PathButton key={p} path={p} onPick={pick} />
                      ))}
                    </span>
                  ),
                },
                { id: "runs", header: "Runs", align: "right", cell: (c) => c.runs },
                { id: "ids", header: "Supporting runs", cell: (c) => <RunLinks ids={c.run_ids} /> },
              ]}
              rows={query.data.changed_together}
              rowKey={(c) => c.paths.join("\n")}
              empty="No files changed together in more than one run."
            />
          </Card>
          <Card title="Protected-path activity" padded={false}>
            <DataTable
              caption="Protected-path activity"
              columns={refColumns(pick, true)}
              rows={query.data.protected_activity}
              rowKey={(r) => `${r.run_id}:${r.path}`}
              empty="No run touched a protected path."
            />
          </Card>
          <Card title="Recent activity" padded={false}>
            <DataTable
              caption="Recent file activity"
              columns={refColumns(pick, false)}
              rows={query.data.recent}
              rowKey={(r) => `${r.run_id}:${r.path}`}
              empty="No recent file activity."
            />
          </Card>
        </>
      )}
    </Page>
  );
}
