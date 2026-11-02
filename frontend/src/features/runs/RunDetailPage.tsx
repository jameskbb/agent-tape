import { AlertOctagon, Ban, Download, RotateCcw } from "lucide-react";
import { useState } from "react";
import { Link, useParams, useSearchParams } from "react-router";

import { ApiError, describeError, downloadFromApi } from "@/api/client";
import { useCancelRun, useDecideApproval, useRetryRun, useRunDetail } from "@/api/hooks";
import { TERMINAL_STATUSES, type Approval, type RunDetail } from "@/api/types";
import { runDetailActions, runDetailTabs } from "@/app/registry";
import { Page } from "@/components/layout/Page";
import {
  Badge,
  Button,
  ConfirmDialog,
  Duration,
  EmptyState,
  ErrorState,
  LoadingBlock,
  RelativeTime,
  StatusPill,
  Tabs,
  Unavailable,
  useToast,
  Cost,
} from "@/components/ui";
import { RunHeaderTally } from "@/features/findings/RunHeaderTally";
import { humanize, sourceLabel } from "@/lib/format";

import { useRunEvents } from "./useRunEvents";

const CANCELLABLE = new Set(["queued", "preparing", "running", "waiting_for_approval"]);
const RETRYABLE = new Set(["failed", "timed_out", "cancelled"]);

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="mt-0.5 truncate text-sm text-ink">{children}</dd>
    </div>
  );
}

function UsageFact({ detail }: { detail: RunDetail }) {
  const usage = detail.run.usage;
  if (!usage || (usage.input_tokens === undefined && usage.output_tokens === undefined)) {
    return <Unavailable reason="Not reported" />;
  }
  return (
    <span className="inline-flex items-center gap-1.5">
      {String(usage.input_tokens ?? "?")} in / {String(usage.output_tokens ?? "?")} out
      {usage.simulated ? (
        <Badge
          tone="warning"
          title="The Demo Agent reports simulated numbers, not real model usage"
        >
          Simulated
        </Badge>
      ) : null}
    </span>
  );
}

function ApprovalPanel({ approvals, runId }: { approvals: Approval[]; runId: string }) {
  const pending = approvals.filter((a) => a.status === "pending");
  const decide = useDecideApproval(runId);
  const toast = useToast();
  const [dialog, setDialog] = useState<{
    approval: Approval;
    decision: "approve" | "reject";
  } | null>(null);
  if (pending.length === 0) return null;
  return (
    <section aria-label="Pending approvals" className="flex flex-col gap-2">
      {pending.map((a) => (
        <div
          key={a.id}
          className="flex flex-wrap items-start justify-between gap-3 rounded-[var(--radius-panel)] border border-[color-mix(in_srgb,var(--st-waiting)_45%,transparent)] bg-[var(--st-waiting-bg)] p-4"
        >
          <div className="min-w-0 max-w-[70ch]">
            <p className="font-semibold text-ink">{a.title}</p>
            {a.rationale ? <p className="mt-1 text-sm text-ink-soft">{a.rationale}</p> : null}
            <p className="mt-1 text-xs text-muted">
              {humanize(a.risk_level)} risk, requested by {a.requester}{" "}
              <RelativeTime iso={a.created_at} />
              {a.expires_at ? (
                <>
                  , expires <RelativeTime iso={a.expires_at} />
                </>
              ) : null}
            </p>
          </div>
          <div className="flex gap-2">
            <Button variant="danger" onClick={() => setDialog({ approval: a, decision: "reject" })}>
              Reject
            </Button>
            <Button
              variant="primary"
              onClick={() => setDialog({ approval: a, decision: "approve" })}
            >
              Approve
            </Button>
          </div>
        </div>
      ))}
      <ConfirmDialog
        open={dialog !== null}
        title={dialog?.decision === "approve" ? "Approve this request?" : "Reject this request?"}
        description={
          dialog?.approval.type === "commit"
            ? dialog.decision === "approve"
              ? "AgentTape makes one local commit in the run's isolated worktree. Nothing is pushed or merged."
              : "No commit is made. The worktree keeps its changes."
            : dialog?.decision === "approve"
              ? "The run resumes from where it paused. Work it already finished is not repeated."
              : "The run stops and is marked failed with the reason approval rejected."
        }
        confirmLabel={dialog?.decision === "approve" ? "Approve" : "Reject"}
        tone={dialog?.decision === "reject" ? "danger" : "primary"}
        withNote
        busy={decide.isPending}
        onClose={() => setDialog(null)}
        onConfirm={(note) => {
          if (!dialog) return;
          decide.mutate(
            { id: dialog.approval.id, decision: dialog.decision, note: note || undefined },
            {
              onSuccess: () => {
                toast.show(
                  dialog.decision === "reject"
                    ? "Rejected"
                    : dialog.approval.type === "commit"
                      ? "Approved; the commit result is in the Changes tab"
                      : "Approved; the run is resuming",
                  "success",
                );
                setDialog(null);
              },
              onError: (error) => toast.show(describeError(error), "error"),
            },
          );
        }}
      />
    </section>
  );
}

function Controls({ detail }: { detail: RunDetail }) {
  const { run } = detail;
  const cancel = useCancelRun(run.id);
  const retry = useRetryRun(run.id);
  const toast = useToast();
  const [confirm, setConfirm] = useState<"cancel" | "retry" | null>(null);
  const [exporting, setExporting] = useState(false);
  return (
    <>
      {CANCELLABLE.has(run.status) ? (
        <Button
          variant="danger"
          icon={<Ban className="size-4" aria-hidden />}
          onClick={() => setConfirm("cancel")}
        >
          Cancel run
        </Button>
      ) : null}
      {RETRYABLE.has(run.status) ? (
        <Button
          icon={<RotateCcw className="size-4" aria-hidden />}
          onClick={() => setConfirm("retry")}
        >
          Retry run
        </Button>
      ) : null}
      <Button
        icon={<Download className="size-4" aria-hidden />}
        loading={exporting}
        onClick={() => {
          setExporting(true);
          downloadFromApi(`/runs/${run.id}/export`, `agenttape-run-${run.id}.json`)
            .catch((e: unknown) => toast.show(describeError(e), "error"))
            .finally(() => setExporting(false));
        }}
      >
        Export
      </Button>
      {runDetailActions.map((action) => (
        <span key={action.id} className="contents">
          {action.render({ detail })}
        </span>
      ))}
      <ConfirmDialog
        open={confirm === "cancel"}
        title="Cancel this run?"
        description="A running agent process is stopped (its whole process group). The run keeps its events and is marked cancelled."
        confirmLabel="Cancel run"
        tone="danger"
        busy={cancel.isPending}
        onClose={() => setConfirm(null)}
        onConfirm={() =>
          cancel.mutate(undefined, {
            onSuccess: (r) => {
              toast.show(
                r.status === "cancelling" ? "Stopping the agent" : "Run cancelled",
                "success",
              );
              setConfirm(null);
            },
            onError: (e) => toast.show(describeError(e), "error"),
          })
        }
      />
      <ConfirmDialog
        open={confirm === "retry"}
        title="Retry this run?"
        description="The same immutable configuration runs again as a new attempt on this run."
        confirmLabel="Retry run"
        busy={retry.isPending}
        onClose={() => setConfirm(null)}
        onConfirm={() =>
          retry.mutate(undefined, {
            onSuccess: () => {
              toast.show("Run queued for retry", "success");
              setConfirm(null);
            },
            onError: (e) => toast.show(describeError(e), "error"),
          })
        }
      />
    </>
  );
}

export function RunDetailPage() {
  const { runId = "" } = useParams();
  return <RunDetailView key={runId} runId={runId} />;
}

function RunDetailView({ runId }: { runId: string }) {
  const [params, setParams] = useSearchParams();
  const query = useRunDetail(runId, { live: true });
  const status = query.data?.run.status;
  const live = status !== undefined && !TERMINAL_STATUSES.has(status);
  const events = useRunEvents(runId, status);

  if (query.isPending) {
    return (
      <Page
        title="Run"
        breadcrumb={
          <Link to="/runs" className="hover:text-ink">
            Runs
          </Link>
        }
      >
        <LoadingBlock label="Loading run" />
      </Page>
    );
  }
  if (query.isError) {
    const notFound = query.error instanceof ApiError && query.error.status === 404;
    return (
      <Page
        title={notFound ? "Run not found" : "Run"}
        breadcrumb={
          <Link to="/runs" className="hover:text-ink">
            Runs
          </Link>
        }
      >
        {notFound ? (
          <EmptyState
            title="This run does not exist"
            description="It may have been deleted, for example by resetting the demo data."
            action={
              <Link to="/runs" className="text-accent hover:underline">
                Back to runs
              </Link>
            }
          />
        ) : (
          <ErrorState error={query.error} onRetry={() => void query.refetch()} />
        )}
      </Page>
    );
  }

  const detail = query.data;
  const { run } = detail;
  const tabs = runDetailTabs.filter((t) => t.isAvailable?.(detail) ?? true);
  const current = tabs.find((t) => t.id === params.get("tab")) ?? tabs[0];

  return (
    <Page
      title={run.title}
      breadcrumb={
        <Link to="/runs" className="hover:text-ink">
          Runs
        </Link>
      }
      actions={<Controls detail={detail} />}
    >
      <div className="flex flex-col gap-4 rounded-[var(--radius-panel)] border border-line bg-surface p-4">
        <div className="flex flex-wrap items-center gap-3">
          <StatusPill status={run.status} />
          {live ? (
            <span className="inline-flex items-center gap-1.5 text-sm text-live">
              <span aria-hidden className="at-live-dot size-2 rounded-full bg-live" />
              {events.streaming ? "Streaming live" : "Connecting to live events"}
            </span>
          ) : null}
          {run.queue_position ? (
            <span className="text-sm text-muted">Position {run.queue_position} in the queue</span>
          ) : null}
          {run.is_demo ? <Badge>Demo data</Badge> : null}
          <code className="ml-auto text-xs text-muted" title="Run id">
            {run.id}
          </code>
        </div>
        {run.failure_category ? (
          <div
            role="status"
            className="flex items-start gap-2 rounded-[var(--radius-control)] bg-[var(--st-failed-bg)] px-3 py-2 text-sm"
          >
            <AlertOctagon aria-hidden className="mt-0.5 size-4 shrink-0 text-[var(--st-failed)]" />
            <p>
              <span className="font-semibold text-[var(--st-failed)]">
                {humanize(run.failure_category)}.
              </span>{" "}
              <span className="text-ink-soft">{run.failure_message}</span>
            </p>
          </div>
        ) : null}
        <dl className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-3 lg:grid-cols-6">
          <Fact label="Duration">
            {run.duration_ms !== null ? (
              <Duration ms={run.duration_ms} />
            ) : run.started_at ? (
              <Duration start={run.started_at} live={live} />
            ) : (
              <Unavailable reason="Not started" />
            )}
          </Fact>
          <Fact label="Attempts">
            {run.attempt_count} of {run.max_attempts}
          </Fact>
          <Fact label="Agent">
            {run.agent_profile_name ?? run.adapter_type ?? humanize(run.kind)}
          </Fact>
          <Fact label="Prompt">{run.prompt_name ?? <Unavailable reason="None" />}</Fact>
          <Fact label="Started from">
            {sourceLabel(run.source)}
            {run.created_by ? `, ${run.created_by}` : ""}
          </Fact>
          <Fact label="Created">
            <RelativeTime iso={run.created_at} />
          </Fact>
          <Fact label="Project">{run.project_name ?? <Unavailable reason="None" />}</Fact>
          <Fact label="Token usage">
            <UsageFact detail={detail} />
          </Fact>
          <Fact label="Cost">
            <Cost cost={run.cost} />
          </Fact>
          <Fact label="Events">{detail.event_count}</Fact>
          <RunHeaderTally detail={detail} />
          {run.parent_run_id ? (
            <Fact label={run.parent_relation ? humanize(run.parent_relation) + " of" : "Parent"}>
              <Link to={`/runs/${run.parent_run_id}`} className="text-accent hover:underline">
                {run.parent_run_id.slice(0, 8)}
              </Link>
            </Fact>
          ) : null}
        </dl>
      </div>

      <ApprovalPanel approvals={detail.approvals} runId={run.id} />

      {current ? (
        <Tabs
          label="Run sections"
          items={tabs.map((t) => ({
            id: t.id,
            label: t.label,
            count: t.count?.(detail),
          }))}
          value={current.id}
          onChange={(id) =>
            setParams(
              (prev) => {
                const next = new URLSearchParams(prev);
                next.set("tab", id);
                return next;
              },
              { replace: true },
            )
          }
        >
          {current.render({ detail, events: events.events, eventsLoading: events.loading, live })}
        </Tabs>
      ) : null}
      {events.error ? <ErrorState title="Events could not be loaded" error={events.error} /> : null}
    </Page>
  );
}
