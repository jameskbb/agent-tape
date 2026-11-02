import { useQuery } from "@tanstack/react-query";
import { ShieldCheck } from "lucide-react";
import { useState } from "react";
import { Link, useSearchParams } from "react-router";

import { api, describeError } from "@/api/client";
import { useDecideApproval } from "@/api/hooks";
import type { Approval, Page as PageOf } from "@/api/types";
import { Page } from "@/components/layout/Page";
import {
  Badge,
  Button,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  JsonInspector,
  LoadingBlock,
  RelativeTime,
  Tabs,
  useToast,
} from "@/components/ui";
import { humanize, shortId } from "@/lib/format";

const VIEWS = {
  pending: ["pending"],
  resolved: ["approved", "rejected", "expired", "cancelled"],
} as const;

const TYPE_LABELS: Record<string, string> = {
  run_continue: "Continue the run",
  workflow_node: "Workflow step",
  commit: "Git commit",
  git_commit: "Git commit",
  elevated_command: "Elevated command",
  protected_path: "Protected path write",
  budget_increase: "Budget increase",
  evaluation_override: "Evaluation override",
};

const RISK_TONE = {
  low: "neutral",
  medium: "warning",
  high: "danger",
  critical: "danger",
} as const;
const STATUS_TONE = {
  pending: "violet",
  approved: "success",
  rejected: "danger",
  expired: "warning",
  cancelled: "neutral",
} as const;

const PAGE_SIZE = 25;

function useApprovals(view: keyof typeof VIEWS, offset: number) {
  return useQuery({
    queryKey: ["approvals", { view, offset }],
    queryFn: () =>
      api<PageOf<Approval>>("/approvals", {
        query: { status: [...VIEWS[view]], limit: PAGE_SIZE, offset },
      }),
    refetchInterval: 4000,
  });
}

function ApprovalCard({
  approval,
  onDecide,
}: {
  approval: Approval;
  onDecide: (a: Approval, d: "approve" | "reject") => void;
}) {
  const a = approval;
  const pending = a.status === "pending";
  return (
    <li
      className="rounded-[var(--radius-panel)] border border-line bg-surface p-4 data-[pending=true]:border-[color-mix(in_srgb,var(--st-waiting)_45%,transparent)]"
      data-pending={pending}
      aria-label={a.title}
    >
      <div className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <Badge tone={STATUS_TONE[a.status]}>{humanize(a.status)}</Badge>
            <Badge>{TYPE_LABELS[a.type] ?? humanize(a.type)}</Badge>
            <Badge tone={RISK_TONE[a.risk_level]}>{humanize(a.risk_level)} risk</Badge>
            {a.is_demo ? <Badge tone="live">Demo</Badge> : null}
          </div>
          <p className="mt-2 font-semibold text-ink">{a.title}</p>
          {a.rationale ? (
            <p className="mt-1 max-w-[75ch] text-sm text-ink-soft">{a.rationale}</p>
          ) : null}
          <p className="mt-2 text-xs text-muted">
            Requested by {a.requester} <RelativeTime iso={a.created_at} />
            {a.run_id ? (
              <>
                {" "}
                for run{" "}
                <Link className="text-accent underline" to={`/runs/${a.run_id}`}>
                  {shortId(a.run_id)}
                </Link>
                {a.node_key ? ` at node ${a.node_key}` : ""}
              </>
            ) : null}
            {pending && a.expires_at ? (
              <>
                . Expires <RelativeTime iso={a.expires_at} />
              </>
            ) : null}
            {!pending ? (
              <>
                . {humanize(a.status)} by {a.reviewer ?? "unknown"}
                {a.resolved_at ? (
                  <>
                    {" "}
                    <RelativeTime iso={a.resolved_at} />
                  </>
                ) : null}
                {a.reviewer_note ? `: ${a.reviewer_note}` : ""}
              </>
            ) : null}
          </p>
        </div>
        {pending ? (
          <div className="flex gap-2">
            <Button variant="danger" onClick={() => onDecide(a, "reject")}>
              Reject
            </Button>
            <Button variant="primary" onClick={() => onDecide(a, "approve")}>
              Approve
            </Button>
          </div>
        ) : null}
      </div>
      {Object.keys(a.requested_action).length ? (
        <div className="mt-3">
          <JsonInspector label="Requested action" value={a.requested_action} defaultDepth={1} />
        </div>
      ) : null}
    </li>
  );
}

/** One inbox for every approval across runs and workflows. Decisions are audited. */
export function ApprovalsPage() {
  const toast = useToast();
  const [params, setParams] = useSearchParams();
  const view = params.get("view") === "resolved" ? "resolved" : "pending";
  const offset = Number(params.get("offset") ?? 0) || 0;
  const approvals = useApprovals(view, offset);
  const decide = useDecideApproval();
  const [dialog, setDialog] = useState<{
    approval: Approval;
    decision: "approve" | "reject";
  } | null>(null);

  return (
    <Page
      title="Approvals"
      description="Risky steps wait here for a person: workflow approval nodes, commits, elevated commands, protected-path writes, budget increases, and evaluation overrides. Every decision is recorded in the audit log."
    >
      <Tabs
        label="Approval views"
        value={view}
        onChange={(v) => setParams(v === "pending" ? {} : { view: v })}
        items={[
          {
            id: "pending",
            label: "Waiting for you",
            count: view === "pending" ? approvals.data?.total : undefined,
          },
          { id: "resolved", label: "Decided" },
        ]}
      >
        {approvals.isPending ? (
          <LoadingBlock />
        ) : approvals.isError ? (
          <ErrorState error={approvals.error} onRetry={() => void approvals.refetch()} />
        ) : approvals.data.items.length === 0 ? (
          <EmptyState
            icon={<ShieldCheck className="size-6" />}
            title={view === "pending" ? "Nothing is waiting for approval" : "No decisions yet"}
            description={
              view === "pending"
                ? "When a run or workflow reaches a step that needs a person, it pauses and appears here."
                : "Approved, rejected, expired, and cancelled requests are listed here."
            }
          />
        ) : (
          <div className="flex flex-col gap-3">
            <ul className="flex flex-col gap-3" aria-label="Approval requests">
              {approvals.data.items.map((a) => (
                <ApprovalCard
                  key={a.id}
                  approval={a}
                  onDecide={(approval, decision) => setDialog({ approval, decision })}
                />
              ))}
            </ul>
            {approvals.data.total > PAGE_SIZE ? (
              <div className="flex items-center gap-2 text-sm text-muted">
                <Button
                  size="sm"
                  disabled={offset === 0}
                  onClick={() =>
                    setParams({
                      ...(view === "resolved" ? { view } : {}),
                      offset: String(Math.max(0, offset - PAGE_SIZE)),
                    })
                  }
                >
                  Newer
                </Button>
                <Button
                  size="sm"
                  disabled={approvals.data.next_offset === null}
                  onClick={() =>
                    setParams({
                      ...(view === "resolved" ? { view } : {}),
                      offset: String(approvals.data.next_offset ?? 0),
                    })
                  }
                >
                  Older
                </Button>
                <span>
                  {offset + 1} to {Math.min(offset + PAGE_SIZE, approvals.data.total)} of{" "}
                  {approvals.data.total}
                </span>
              </div>
            ) : null}
          </div>
        )}
      </Tabs>
      <ConfirmDialog
        open={dialog !== null}
        title={dialog?.decision === "approve" ? "Approve this request?" : "Reject this request?"}
        description={
          dialog?.decision === "approve"
            ? "The paused run or workflow resumes. Steps that already finished are not repeated."
            : "The waiting step fails with the reason approval rejected."
        }
        confirmLabel={dialog?.decision === "approve" ? "Approve" : "Reject"}
        tone={dialog?.decision === "reject" ? "danger" : "primary"}
        withNote
        notePlaceholder="Optional note for the audit log"
        busy={decide.isPending}
        onClose={() => setDialog(null)}
        onConfirm={(note) => {
          if (!dialog) return;
          decide.mutate(
            { id: dialog.approval.id, decision: dialog.decision, note: note || undefined },
            {
              onSuccess: () => {
                toast.show(dialog.decision === "approve" ? "Approved" : "Rejected", "success");
                setDialog(null);
                void approvals.refetch();
              },
              onError: (error) => toast.show(describeError(error), "error"),
            },
          );
        }}
      />
    </Page>
  );
}
