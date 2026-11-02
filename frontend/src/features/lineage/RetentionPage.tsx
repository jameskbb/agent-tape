import { Eraser, ScanSearch } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";

import { describeError } from "@/api/client";
import { Page } from "@/components/layout/Page";
import {
  Badge,
  Button,
  Card,
  ConfirmDialog,
  DataTable,
  ErrorState,
  LoadingBlock,
  RelativeTime,
  StatusPill,
  useToast,
  type Column,
} from "@/components/ui";
import { formatBytes, formatDateTime, humanize } from "@/lib/format";

import {
  useExecuteRetention,
  usePreviewRetention,
  useRetentionSettings,
  useSaveRetention,
  type RetentionPreview,
  type RetentionResult,
  type RetentionSettings,
} from "./api";

const FIELD =
  "rounded-[var(--radius-control)] border border-line-strong bg-surface px-2.5 py-1.5 text-base text-ink";
const FINISHED = ["succeeded", "failed", "timed_out", "cancelled", "skipped"];

type RunItem = RetentionPreview["runs"][number];

const runColumns: Column<RunItem>[] = [
  { id: "status", header: "Status", cell: (r) => <StatusPill status={r.status} size="sm" /> },
  {
    id: "title",
    header: "Run",
    cell: (r) => (
      <span>
        <Link to={`/runs/${r.run_id}`} className="font-medium hover:text-accent">
          {r.title}
        </Link>
        {r.is_demo ? <Badge className="ml-2">Demo</Badge> : null}
      </span>
    ),
  },
  { id: "finished", header: "Finished", cell: (r) => <RelativeTime iso={r.finished_at} /> },
  {
    id: "size",
    header: "Size",
    align: "right",
    cell: (r) => (
      <span
        className="tabular-nums"
        title={`Artifacts ${formatBytes(r.artifact_bytes)}, logs ${formatBytes(r.log_bytes)}, workspace ${formatBytes(r.worktree_bytes)}`}
      >
        {formatBytes(r.total_bytes)}
      </span>
    ),
  },
  {
    id: "action",
    header: "Cleanup",
    cell: (r) =>
      r.blocked ? (
        <span className="text-sm text-[var(--st-cancelling)]">Kept: {r.blocked}</span>
      ) : (
        <span className="text-sm">Delete</span>
      ),
  },
];

function Settings({
  value,
  onChange,
}: {
  value: RetentionSettings;
  onChange: (v: RetentionSettings) => void;
}) {
  const toggle = (key: keyof RetentionSettings) => (e: React.ChangeEvent<HTMLInputElement>) =>
    onChange({ ...value, [key]: e.target.checked });
  return (
    <div className="grid gap-4 md:grid-cols-2">
      <label className="flex flex-col gap-1 text-sm">
        <span className="text-muted">Delete finished runs older than (days)</span>
        <input
          type="number"
          min={1}
          max={3650}
          className={FIELD}
          value={value.max_age_days}
          onChange={(e) => onChange({ ...value, max_age_days: Number(e.target.value) || 1 })}
        />
      </label>
      <label className="flex flex-col gap-1 text-sm">
        <span className="text-muted">Always keep runs tagged</span>
        <input
          className={FIELD}
          value={value.keep_tag}
          onChange={(e) => onChange({ ...value, keep_tag: e.target.value })}
        />
      </label>
      <fieldset className="flex flex-col gap-1.5 text-sm">
        <legend className="mb-1 text-muted">Statuses to clean up</legend>
        <div className="flex flex-wrap gap-x-4 gap-y-1">
          {FINISHED.map((s) => (
            <label key={s} className="flex items-center gap-1.5">
              <input
                type="checkbox"
                checked={value.statuses.includes(s)}
                onChange={(e) =>
                  onChange({
                    ...value,
                    statuses: e.target.checked
                      ? [...value.statuses, s]
                      : value.statuses.filter((x) => x !== s),
                  })
                }
              />
              {humanize(s)}
            </label>
          ))}
        </div>
      </fieldset>
      <div className="flex flex-col gap-1.5 text-sm">
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={value.include_demo} onChange={toggle("include_demo")} />
          Include demo runs
        </label>
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={value.remove_worktrees}
            onChange={toggle("remove_worktrees")}
          />
          Remove clean isolated worktrees with their runs
        </label>
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={value.include_orphans}
            onChange={toggle("include_orphans")}
          />
          Include leftover directories no run refers to
        </label>
      </div>
    </div>
  );
}

function Preview({
  plan,
  onExecuted,
}: {
  plan: RetentionPreview;
  onExecuted: (r: RetentionResult) => void;
}) {
  const execute = useExecuteRetention();
  const toast = useToast();
  const [confirm, setConfirm] = useState(false);
  const runIds = plan.runs.filter((r) => !r.blocked).map((r) => r.run_id);
  const orphanPaths = plan.orphans.filter((o) => !o.blocked).map((o) => o.path);
  const nothing = runIds.length === 0 && orphanPaths.length === 0;
  return (
    <Card
      title="Cleanup preview"
      description={`Dry run from ${formatDateTime(plan.generated_at)}. Nothing has been deleted.`}
      actions={
        <Button
          variant="danger"
          icon={<Eraser className="size-4" aria-hidden />}
          disabled={nothing}
          onClick={() => setConfirm(true)}
        >
          Delete {runIds.length} run{runIds.length === 1 ? "" : "s"}
          {orphanPaths.length ? ` and ${orphanPaths.length} leftover` : ""}
        </Button>
      }
    >
      <div className="flex flex-col gap-4">
        <p className="text-sm" data-testid="retention-summary">
          {nothing
            ? "Nothing would be deleted with these settings."
            : `${runIds.length} run${runIds.length === 1 ? "" : "s"} finished before ${formatDateTime(plan.cutoff)} and ${orphanPaths.length} leftover director${orphanPaths.length === 1 ? "y" : "ies"} would be deleted, freeing about ${formatBytes(plan.total_bytes)}.`}
          {plan.blocked_runs
            ? ` ${plan.blocked_runs} run${plan.blocked_runs === 1 ? " is" : "s are"} kept because deleting would lose work.`
            : ""}
        </p>
        {plan.runs.length ? (
          <DataTable
            caption="Runs in the cleanup preview"
            columns={runColumns}
            rows={plan.runs}
            rowKey={(r) => r.run_id}
          />
        ) : null}
        {plan.orphans.length ? (
          <div>
            <h3 className="text-sm font-medium">Leftover directories</h3>
            <ul className="mt-1 flex flex-col gap-1 text-sm">
              {plan.orphans.map((o) => (
                <li key={o.path} className="flex flex-wrap items-center gap-2">
                  <Badge>{humanize(o.kind)}</Badge>
                  <code className="min-w-0 truncate">{o.path}</code>
                  <span className="text-muted tabular-nums">{formatBytes(o.bytes)}</span>
                  {o.blocked ? (
                    <span className="text-[var(--st-cancelling)]">Kept: {o.blocked}</span>
                  ) : null}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </div>
      <ConfirmDialog
        open={confirm}
        title="Delete this data?"
        description="Only what the preview lists is deleted, and only if it is still eligible: runs with their events, artifacts, and logs, plus the listed leftover directories inside the AgentTape data directory. This cannot be undone."
        confirmLabel="Delete"
        tone="danger"
        busy={execute.isPending}
        onClose={() => setConfirm(false)}
        onConfirm={() =>
          execute.mutate(
            { run_ids: runIds, orphan_paths: orphanPaths },
            {
              onSuccess: (r) => {
                setConfirm(false);
                toast.show(`Deleted ${r.deleted_runs.length} runs`, "success");
                onExecuted(r);
              },
              onError: (e) => toast.show(describeError(e), "error"),
            },
          )
        }
      />
    </Card>
  );
}

function Editor({ initial }: { initial: RetentionSettings }) {
  const [value, setValue] = useState(initial);
  const save = useSaveRetention();
  const preview = usePreviewRetention();
  const toast = useToast();
  const [result, setResult] = useState<RetentionResult | null>(null);
  return (
    <>
      <Card
        title="Retention rules"
        description="Cleanup only ever touches data AgentTape created: runs, their artifacts and logs, and its own worktrees. Worktrees with uncommitted work are never removed."
      >
        <form
          className="flex flex-col gap-4"
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate(value, {
              onSuccess: () => toast.show("Retention settings saved", "success"),
              onError: (err) => toast.show(describeError(err), "error"),
            });
          }}
        >
          <Settings value={value} onChange={setValue} />
          <div className="flex flex-wrap gap-2">
            <Button type="submit" variant="primary" loading={save.isPending}>
              Save settings
            </Button>
            <Button
              icon={<ScanSearch className="size-4" aria-hidden />}
              loading={preview.isPending}
              onClick={() => {
                setResult(null);
                preview.mutate(value, {
                  onError: (err) => toast.show(describeError(err), "error"),
                });
              }}
            >
              Preview cleanup
            </Button>
          </div>
        </form>
      </Card>
      {result ? (
        <Card title="Cleanup finished">
          <p className="text-sm">
            Deleted {result.deleted_runs.length} run{result.deleted_runs.length === 1 ? "" : "s"}{" "}
            and {result.removed_paths.length} path{result.removed_paths.length === 1 ? "" : "s"},
            freeing about {formatBytes(result.freed_bytes)}.
          </p>
          {result.skipped.length ? (
            <ul className="mt-2 list-disc pl-5 text-sm text-muted">
              {result.skipped.map((s) => (
                <li key={s.run_id ?? s.path}>
                  Skipped {s.run_id ? s.run_id.slice(0, 8) : s.path}: {s.reason}
                </li>
              ))}
            </ul>
          ) : null}
        </Card>
      ) : preview.data ? (
        <Preview
          plan={preview.data}
          onExecuted={(r) => {
            setResult(r);
            preview.reset();
          }}
        />
      ) : null}
    </>
  );
}

export function RetentionPage() {
  const settings = useRetentionSettings();
  return (
    <Page
      title="Data retention"
      breadcrumb={
        <Link to="/settings" className="hover:text-ink">
          Settings
        </Link>
      }
      description="Decide how long finished runs are kept. Preview what a cleanup would delete, then run it."
    >
      {settings.isPending ? (
        <LoadingBlock />
      ) : settings.isError ? (
        <ErrorState error={settings.error} onRetry={() => void settings.refetch()} />
      ) : (
        <Editor initial={settings.data} />
      )}
    </Page>
  );
}
