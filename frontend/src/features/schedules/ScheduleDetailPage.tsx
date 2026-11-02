import { Pause, Pencil, Play, Trash2, Zap } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

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

import {
  useDeleteSchedule,
  useRunNow,
  useSchedule,
  useScheduleAction,
  useScheduleHistory,
  useSchedulePreview,
  useScheduleRuns,
  type Occurrence,
  type Schedule,
} from "./api";
import { NextRunsReel, OccurrenceBadge, SchedulerStatusNote } from "./parts";
import { CONCURRENCY_COPY, describeTiming, formatLocal, KIND_LABELS, MISFIRE_COPY } from "./timing";

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="mt-0.5 text-sm break-words text-ink">{children}</dd>
    </div>
  );
}

const historyColumns: Column<Occurrence>[] = [
  {
    id: "when",
    header: "Due (schedule time)",
    cell: (o) => (
      <span className="text-sm tabular-nums">
        {formatLocal(o.occurrence_local) ?? o.occurrence_at}
      </span>
    ),
  },
  { id: "status", header: "Outcome", cell: (o) => <OccurrenceBadge status={o.status} /> },
  {
    id: "run",
    header: "Run",
    cell: (o) =>
      o.run_id ? (
        <span className="inline-flex items-center gap-2">
          {o.run_status ? <StatusPill status={o.run_status} size="sm" /> : null}
          <Link to={`/runs/${o.run_id}`} className="text-accent hover:underline">
            <code>{o.run_id.slice(0, 8)}</code>
          </Link>
        </span>
      ) : (
        <span className="text-faint">No run</span>
      ),
  },
  {
    id: "note",
    header: "Note",
    cell: (o) => <span className="text-sm text-muted">{o.reason ?? ""}</span>,
  },
];

export function ScheduleDetailPage() {
  const { scheduleId = "" } = useParams();
  const schedule = useSchedule(scheduleId);
  if (schedule.isPending) return <LoadingBlock label="Loading schedule" />;
  if (schedule.isError)
    return (
      <ErrorState
        error={schedule.error}
        resource="schedule"
        backTo="/schedules"
        backLabel="Back to schedules"
        onRetry={() => void schedule.refetch()}
      />
    );
  return <ScheduleDetail schedule={schedule.data} />;
}

function ScheduleDetail({ schedule: s }: { schedule: Schedule }) {
  const navigate = useNavigate();
  const toast = useToast();
  const runNow = useRunNow(s.id);
  const action = useScheduleAction(s.id);
  const remove = useDeleteSchedule(s.id);
  const [historyOffset, setHistoryOffset] = useState(0);
  const history = useScheduleHistory(s.id, historyOffset);
  const runs = useScheduleRuns(s.id);
  const [dialog, setDialog] = useState<"run" | "delete" | null>(null);
  const preview = useSchedulePreview(
    s.enabled
      ? {
          kind: s.kind,
          timezone: s.timezone,
          cron_expression: s.cron_expression,
          interval_seconds: s.interval_seconds,
          run_at: s.run_at,
          start_at: s.start_at ?? (s.kind === "interval" ? s.created_at : null),
          end_at: s.end_at,
          count: 5,
        }
      : null,
  );
  const busy = s.active_runs > 0;

  return (
    <Page
      title={s.name}
      breadcrumb={
        <Link to="/schedules" className="hover:text-ink">
          Schedules
        </Link>
      }
      description={s.description}
      actions={
        <>
          <Button
            variant="primary"
            icon={<Zap aria-hidden className="size-4" />}
            onClick={() => setDialog("run")}
          >
            Run now
          </Button>
          <Button
            icon={
              s.enabled ? (
                <Pause aria-hidden className="size-4" />
              ) : (
                <Play aria-hidden className="size-4" />
              )
            }
            loading={action.isPending}
            onClick={() =>
              action.mutate(s.enabled ? "pause" : "resume", {
                onSuccess: () =>
                  toast.show(s.enabled ? "Schedule paused" : "Schedule resumed", "success"),
                onError: (e) => toast.show(describeError(e), "error"),
              })
            }
          >
            {s.enabled ? "Pause" : "Resume"}
          </Button>
          <Button
            icon={<Pencil aria-hidden className="size-4" />}
            onClick={() => void navigate(`/schedules/${s.id}/edit`)}
          >
            Edit
          </Button>
          <Button
            variant="danger"
            icon={<Trash2 aria-hidden className="size-4" />}
            onClick={() => setDialog("delete")}
          >
            Delete
          </Button>
        </>
      }
    >
      <SchedulerStatusNote />
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_22rem]">
        <div className="flex min-w-0 flex-col gap-6">
          <Card>
            <dl className="grid grid-cols-2 gap-x-6 gap-y-4 md:grid-cols-3">
              <Fact label="State">
                {s.enabled ? <Badge tone="success">Enabled</Badge> : <Badge>Paused</Badge>}
              </Fact>
              <Fact label="When">{describeTiming(s)}</Fact>
              <Fact label="Type and zone">
                {KIND_LABELS[s.kind]} in {s.timezone}
              </Fact>
              <Fact label="Next run">
                {!s.enabled ? (
                  <span className="text-faint">Paused: nothing runs until you resume</span>
                ) : s.next_run_at ? (
                  <>
                    {formatLocal(s.next_run_local)} (<RelativeTime iso={s.next_run_at} />)
                  </>
                ) : (
                  <span className="text-faint">No more runs</span>
                )}
              </Fact>
              <Fact label="Last run">
                {s.last_run_id ? (
                  <Link
                    to={`/runs/${s.last_run_id}`}
                    className="inline-flex items-center gap-2 hover:text-accent"
                  >
                    {s.last_run_status ? <StatusPill status={s.last_run_status} size="sm" /> : null}
                    <RelativeTime iso={s.last_run_at} />
                  </Link>
                ) : (
                  <span className="text-faint">Never run</span>
                )}
              </Fact>
              <Fact label="Active right now">
                {s.active_runs} run{s.active_runs === 1 ? "" : "s"}
                {s.queued_occurrences ? `, ${s.queued_occurrences} waiting` : ""}
              </Fact>
              <Fact label="Prompt">
                {s.prompt_template_id ? (
                  <>
                    {s.target_name ?? "Deleted"}
                    <span className="block text-xs text-muted">
                      {s.prompt_version_id ? "Pinned version" : "Latest version at each run"}
                    </span>
                  </>
                ) : (
                  (s.target_name ?? "Deleted")
                )}
              </Fact>
              <Fact label="Agent">
                {s.agent_profile_name ??
                  (s.target_type === "workflow" ? "Set by the workflow" : "Deleted")}
              </Fact>
              <Fact label="Project">{s.project_name ?? "None"}</Fact>
              <Fact label="Missed runs">{MISFIRE_COPY[s.misfire_policy].label}</Fact>
              <Fact label="If still running">
                {CONCURRENCY_COPY[s.concurrency_policy].label}
                {s.concurrency_policy === "queue" ? (
                  <span className="block text-xs text-muted">Up to {s.max_queued} waiting</span>
                ) : null}
              </Fact>
              {s.start_at_local || s.end_at_local ? (
                <Fact label="Active window">
                  {formatLocal(s.start_at_local) ?? "Any time"} to{" "}
                  {formatLocal(s.end_at_local) ?? "no end"}
                </Fact>
              ) : null}
            </dl>
            {Object.keys(s.inputs).length ? (
              <div className="mt-4 border-t border-line pt-3">
                <p className="text-xs text-muted">Inputs</p>
                <ul className="mt-1 flex flex-wrap gap-2 text-sm">
                  {Object.entries(s.inputs).map(([k, v]) => (
                    <li key={k} className="rounded bg-sunken px-2 py-0.5">
                      <code>{k}</code> = {String(v)}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </Card>

          <Card
            title="History"
            description="Every due time and what happened to it."
            padded={false}
          >
            {history.isPending ? (
              <div className="p-4">
                <LoadingBlock />
              </div>
            ) : history.isError ? (
              <div className="p-4">
                <ErrorState error={history.error} onRetry={() => void history.refetch()} />
              </div>
            ) : (
              <div className="p-4">
                <DataTable
                  caption="Schedule history"
                  columns={historyColumns}
                  rows={history.data.items}
                  rowKey={(o) => o.id}
                  empty="No occurrences yet. Press Run now to try it, or wait for the next run."
                  pagination={{
                    offset: historyOffset,
                    limit: 20,
                    total: history.data.total,
                    onChange: setHistoryOffset,
                  }}
                />
              </div>
            )}
          </Card>

          {runs.data && runs.data.total > 0 ? (
            <Card title="Recent runs" padded={false}>
              <ul className="divide-y divide-line">
                {runs.data.items.map((r) => (
                  <li key={r.id}>
                    <Link
                      to={`/runs/${r.id}`}
                      className="flex flex-wrap items-center justify-between gap-2 px-4 py-2.5 hover:bg-sunken"
                    >
                      <span className="flex items-center gap-2">
                        <StatusPill status={r.status} size="sm" />
                        <span className="text-ink">{r.title}</span>
                      </span>
                      <RelativeTime iso={r.created_at} className="text-sm text-muted" />
                    </Link>
                  </li>
                ))}
              </ul>
            </Card>
          ) : null}
        </div>
        <aside className="lg:self-start">
          <Card title="Next runs" description={`In ${s.timezone}.`}>
            {s.enabled ? (
              <NextRunsReel
                preview={preview.data}
                loading={preview.isFetching}
                error={preview.error}
                emptyHint="This schedule has no more runs."
              />
            ) : (
              <p className="text-sm text-muted">
                Paused. Resume the schedule to see upcoming runs.
              </p>
            )}
          </Card>
        </aside>
      </div>

      <ConfirmDialog
        open={dialog === "run"}
        title="Run this schedule now?"
        description={
          <>
            A run starts right away with the schedule's prompt, agent, and inputs, and is recorded
            in its history.
            {busy
              ? ` A run from this schedule is still active, so the "${CONCURRENCY_COPY[s.concurrency_policy].label}" rule applies.`
              : null}
          </>
        }
        confirmLabel="Run now"
        busy={runNow.isPending}
        onClose={() => setDialog(null)}
        onConfirm={() =>
          runNow.mutate(false, {
            onSuccess: (result) => {
              setDialog(null);
              toast.show(result.message, result.run_id ? "success" : "info");
              if (result.run_id) void navigate(`/runs/${result.run_id}`);
            },
            onError: (e) => toast.show(describeError(e), "error"),
          })
        }
      />
      <ConfirmDialog
        open={dialog === "delete"}
        title="Delete this schedule?"
        description="Its history is deleted too. Runs it already created stay in Runs."
        confirmLabel="Delete schedule"
        tone="danger"
        busy={remove.isPending}
        onClose={() => setDialog(null)}
        onConfirm={() =>
          remove.mutate(undefined, {
            onSuccess: () => {
              toast.show("Schedule deleted", "success");
              void navigate("/schedules");
            },
            onError: (e) => toast.show(describeError(e), "error"),
          })
        }
      />
    </Page>
  );
}
