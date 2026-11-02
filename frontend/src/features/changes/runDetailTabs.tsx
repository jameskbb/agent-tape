/**
 * The "Changes" run-detail tab: starting revision and dirty-tree warning, the
 * isolated worktree, changed files with a syntax-coloured diff viewer, protected-path
 * warnings, validation results, and the approved local commit.
 */
import { AlertTriangle, GitBranch, GitCommitHorizontal, ShieldAlert, Trash2 } from "lucide-react";
import { useState } from "react";

import { describeError } from "@/api/client";
import { TERMINAL_STATUSES, type RunDetail } from "@/api/types";
import type { RunDetailTab } from "@/app/registry";
import {
  Badge,
  Button,
  Card,
  ConfirmDialog,
  DataTable,
  EmptyState,
  ErrorState,
  LoadingBlock,
  RelativeTime,
  Unavailable,
  useToast,
  type Column,
} from "@/components/ui";
import { humanize, text } from "@/lib/format";

import {
  useCleanupWorktree,
  useRequestCommit,
  useRunGit,
  type ChangedFile,
  type CleanupResult,
  type RunGitView,
} from "./api";
import { DiffViewer } from "./DiffViewer";

const EVAL_TONE = {
  pass: "success",
  fail: "danger",
  warning: "warning",
  error: "danger",
  skipped: "neutral",
} as const;

function short(sha: string | null | undefined): string {
  return sha ? sha.slice(0, 10) : "";
}

function Callout({
  tone,
  icon,
  children,
}: {
  tone: "warning" | "danger";
  icon: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div
      role="status"
      className={
        tone === "warning"
          ? "flex items-start gap-2 rounded-[var(--radius-control)] bg-[var(--st-cancelling-bg)] px-3 py-2 text-sm"
          : "flex items-start gap-2 rounded-[var(--radius-control)] bg-[var(--st-failed-bg)] px-3 py-2 text-sm"
      }
    >
      <span
        className={
          tone === "warning"
            ? "mt-0.5 text-[var(--st-cancelling)]"
            : "mt-0.5 text-[var(--st-failed)]"
        }
      >
        {icon}
      </span>
      <div className="min-w-0 text-ink-soft">{children}</div>
    </div>
  );
}

function Revision({ view }: { view: RunGitView }) {
  const start = view.starting;
  const ws = view.workspace;
  return (
    <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-2 lg:grid-cols-4">
      <div>
        <dt className="text-xs text-muted">Starting revision</dt>
        <dd className="mt-0.5 text-sm">
          {start?.is_repo && start.head ? (
            <span className="inline-flex flex-wrap items-center gap-1.5">
              <code title={start.head}>{short(start.head)}</code>
              <span className="text-muted">on {start.branch ?? "a detached HEAD"}</span>
            </span>
          ) : start && !start.is_repo ? (
            <Unavailable reason="Not a Git repository" />
          ) : (
            <Unavailable reason="No project" />
          )}
        </dd>
      </div>
      <div>
        <dt className="text-xs text-muted">Ran in</dt>
        <dd className="mt-0.5 text-sm">
          {ws?.isolated ? (
            <span className="inline-flex items-center gap-1.5">
              <GitBranch aria-hidden className="size-3.5 text-accent" />
              <span>Isolated worktree</span>
            </span>
          ) : ws?.status === "shared" ? (
            "The project directory"
          ) : ws?.status === "scratch" ? (
            "A scratch directory"
          ) : (
            <Unavailable reason="Not recorded" />
          )}
        </dd>
      </div>
      <div className="min-w-0">
        <dt className="text-xs text-muted">Branch</dt>
        <dd className="mt-0.5 truncate text-sm">
          {ws?.isolated && ws.branch ? (
            <code title={ws.path ?? undefined}>{ws.branch}</code>
          ) : (
            <Unavailable reason="Not isolated" />
          )}
        </dd>
      </div>
      <div>
        <dt className="text-xs text-muted">Worktree</dt>
        <dd className="mt-0.5 text-sm">
          {!ws?.isolated ? (
            <Unavailable reason="Not isolated" />
          ) : ws.status === "removed" ? (
            <span className="text-muted">
              Removed <RelativeTime iso={ws.removed_at} />
            </span>
          ) : ws.uncommitted_count === null ? (
            <Unavailable reason="Could not be read" />
          ) : ws.uncommitted_count === 0 ? (
            "Clean"
          ) : (
            `${ws.uncommitted_count} uncommitted change${ws.uncommitted_count === 1 ? "" : "s"}`
          )}
        </dd>
      </div>
    </dl>
  );
}

const fileColumns: Column<ChangedFile>[] = [
  {
    id: "type",
    header: "Change",
    cell: (c) => (
      <Badge
        tone={
          c.change_type === "added"
            ? "success"
            : c.change_type === "deleted"
              ? "danger"
              : c.change_type === "renamed"
                ? "violet"
                : "neutral"
        }
      >
        {humanize(c.change_type)}
      </Badge>
    ),
  },
  {
    id: "path",
    header: "File",
    cell: (c) => (
      <span>
        <code>{c.path}</code>
        {c.old_path ? <span className="block text-xs text-muted">from {c.old_path}</span> : null}
        {c.is_protected ? (
          <Badge tone="warning" className="ml-2">
            Protected path
          </Badge>
        ) : null}
      </span>
    ),
  },
  {
    id: "lines",
    header: "Lines",
    align: "right",
    cell: (c) =>
      c.is_binary ? (
        <span className="text-muted">binary</span>
      ) : (
        <span className="tabular-nums">
          <span className="text-[var(--st-succeeded)]">+{c.insertions ?? "?"}</span>{" "}
          <span className="text-[var(--st-failed)]">-{c.deletions ?? "?"}</span>
        </span>
      ),
  },
];

function Validations({ view }: { view: RunGitView }) {
  if (view.validation_commands.length === 0 && view.validations.length === 0) {
    return (
      <p className="text-sm text-muted">
        No validation commands are configured for this project. Add them in the project's settings
        to check every change (for example a test or lint command).
      </p>
    );
  }
  if (view.validations.length === 0) {
    return (
      <p className="text-sm text-muted">
        Validation commands run after an attempt changes files; none ran for this run.
      </p>
    );
  }
  return (
    <ul className="flex flex-col divide-y divide-line">
      {view.validations.map((v) => {
        const argv = Array.isArray(v.evidence.argv) ? (v.evidence.argv as string[]) : [];
        return (
          <li key={v.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2">
            <Badge tone={EVAL_TONE[v.status]}>{humanize(v.status)}</Badge>
            <code className="min-w-0 flex-1 truncate" title={argv.join(" ")}>
              {argv.join(" ") || v.name}
            </code>
            <span className="text-sm text-muted">{v.explanation}</span>
            {v.duration_ms !== null ? (
              <span className="text-xs text-faint tabular-nums">{v.duration_ms} ms</span>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

function CommitPanel({ view, runFinished }: { view: RunGitView; runFinished: boolean }) {
  const [message, setMessage] = useState(view.commit.suggested_message);
  const request = useRequestCommit(view.run_id);
  const toast = useToast();
  const commits = view.workspace?.commits ?? [];
  const pending = view.commit.pending;
  return (
    <div className="flex flex-col gap-3">
      {commits.length > 0 ? (
        <ul className="flex flex-col gap-1.5">
          {commits.map((c) => (
            <li key={c.sha} className="flex flex-wrap items-center gap-2 text-sm">
              <GitCommitHorizontal aria-hidden className="size-4 text-[var(--st-succeeded)]" />
              <code>{short(c.sha)}</code>
              <span className="font-medium">{c.message.split("\n")[0]}</span>
              <span className="text-muted">
                approved by {c.reviewer ?? "a reviewer"}, <RelativeTime iso={c.created_at} />
              </span>
            </li>
          ))}
        </ul>
      ) : null}
      {pending ? (
        <Callout tone="warning" icon={<AlertTriangle className="size-4" aria-hidden />}>
          <p className="font-medium text-ink">Commit waiting for approval</p>
          <p>
            Nothing is committed until someone approves the request above. Message:{" "}
            <q>{text(pending.requested_action.message, "").split("\n")[0]}</q>
          </p>
        </Callout>
      ) : null}
      {view.commit.available ? (
        <form
          className="flex flex-col gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            request.mutate(message, {
              onSuccess: () => toast.show("Commit requested; it needs approval", "success"),
              onError: (err) => toast.show(describeError(err), "error"),
            });
          }}
        >
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-muted">Commit message</span>
            <textarea
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              rows={4}
              required
              maxLength={5000}
              className="rounded-[var(--radius-control)] border border-line-strong bg-surface px-2.5 py-2 font-mono text-sm text-ink"
            />
          </label>
          <p className="text-xs text-muted">
            Creates an approval request. After approval AgentTape makes one local commit on{" "}
            <code>{view.workspace?.branch}</code>. It never pushes or merges.
          </p>
          <div>
            <Button
              type="submit"
              variant="primary"
              loading={request.isPending}
              disabled={!message.trim()}
              icon={<GitCommitHorizontal className="size-4" aria-hidden />}
            >
              Request commit
            </Button>
          </div>
        </form>
      ) : !pending && view.commit.reason ? (
        <p className="text-sm text-muted">
          {runFinished || !view.workspace?.isolated
            ? view.commit.reason
            : "You can commit once the run has finished."}
        </p>
      ) : null}
    </div>
  );
}

function CleanupControl({ view }: { view: RunGitView }) {
  const cleanup = useCleanupWorktree(view.run_id);
  const toast = useToast();
  const [confirm, setConfirm] = useState(false);
  const [result, setResult] = useState<CleanupResult | null>(null);
  const ws = view.workspace;
  if (!ws?.isolated || ws.status !== "active") return null;
  return (
    <div className="flex flex-col gap-2">
      <div>
        <Button
          variant="danger"
          size="sm"
          icon={<Trash2 className="size-4" aria-hidden />}
          disabled={Boolean(view.commit.pending)}
          onClick={() => setConfirm(true)}
        >
          Remove worktree
        </Button>
      </div>
      {result && !result.removed ? (
        <Callout tone="danger" icon={<AlertTriangle className="size-4" aria-hidden />}>
          <p>{result.message}</p>
          {result.uncommitted.length ? (
            <p className="mt-1 text-xs">
              {result.uncommitted.slice(0, 8).join(", ")}
              {result.uncommitted.length > 8 ? " and more" : ""}
            </p>
          ) : null}
        </Callout>
      ) : null}
      <ConfirmDialog
        open={confirm}
        title="Remove this worktree?"
        description="AgentTape removes its own worktree only if nothing uncommitted would be lost; otherwise it refuses. The branch is kept if it has commits."
        confirmLabel="Remove worktree"
        tone="danger"
        busy={cleanup.isPending}
        onClose={() => setConfirm(false)}
        onConfirm={() =>
          cleanup.mutate(undefined, {
            onSuccess: (r) => {
              setResult(r);
              setConfirm(false);
              toast.show(
                r.removed ? "Worktree removed" : "Cleanup refused",
                r.removed ? "success" : "error",
              );
            },
            onError: (e) => toast.show(describeError(e), "error"),
          })
        }
      />
    </div>
  );
}

function ChangesTab({ detail, live }: { detail: RunDetail; live: boolean }) {
  const query = useRunGit(detail.run.id, { live });
  if (query.isPending) return <LoadingBlock label="Loading changes" />;
  if (query.isError) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  const view = query.data;
  const finished = TERMINAL_STATUSES.has(detail.run.status);
  const protectedFiles = view.files.filter((f) => f.is_protected);
  return (
    <div className="flex flex-col gap-4">
      <Card padded>
        <Revision view={view} />
      </Card>
      {view.starting?.dirty ? (
        <Callout tone="warning" icon={<AlertTriangle className="size-4" aria-hidden />}>
          <p className="font-medium text-ink">
            The repository had {view.starting.dirty_count} uncommitted file
            {view.starting.dirty_count === 1 ? "" : "s"} when the run started
          </p>
          <p>
            {view.workspace?.isolated
              ? "The isolated worktree started from the recorded commit, without them."
              : "The agent worked on top of them; files that were already changed are marked."}{" "}
            <span className="text-muted">{view.starting.dirty_files.slice(0, 6).join(", ")}</span>
          </p>
        </Callout>
      ) : null}
      {protectedFiles.length ? (
        <Callout tone="danger" icon={<ShieldAlert className="size-4" aria-hidden />}>
          <p className="font-medium text-ink">
            {protectedFiles.length} protected path{protectedFiles.length === 1 ? " was" : "s were"}{" "}
            changed
          </p>
          <p>
            {protectedFiles.map((f) => f.path).join(", ")} (rules:{" "}
            {view.protected_patterns.join(", ")})
          </p>
        </Callout>
      ) : null}

      {view.files.length === 0 ? (
        <EmptyState
          title="No file changes recorded"
          description={
            live
              ? "Changes appear here after the attempt finishes."
              : view.starting?.is_repo
                ? "The agent did not change any files in the repository."
                : "This run has no Git project, so there is nothing to diff."
          }
        />
      ) : (
        <>
          <p className="text-sm text-muted">
            {view.summary.files} file{view.summary.files === 1 ? "" : "s"} changed
            {view.attempt_number ? ` in attempt ${view.attempt_number}` : ""},{" "}
            <span className="text-[var(--st-succeeded)]">+{view.summary.insertions}</span>{" "}
            <span className="text-[var(--st-failed)]">-{view.summary.deletions}</span>
            {view.summary.binary_files ? `, ${view.summary.binary_files} binary` : ""}
          </p>
          <DataTable
            caption="Changed files"
            columns={fileColumns}
            rows={view.files}
            rowKey={(f) => f.id}
          />
          <DiffViewer key={view.files.map((f) => f.id).join()} files={view.files} />
        </>
      )}

      <Card title="Validation commands" description="Run in the workdir after the agent's changes.">
        <Validations view={view} />
      </Card>
      {view.workspace?.isolated ? (
        <Card
          title="Local commit"
          description="Commit the worktree's changes to its own branch, after approval."
        >
          <div className="flex flex-col gap-4">
            <CommitPanel view={view} runFinished={finished} />
            <CleanupControl view={view} />
          </div>
        </Card>
      ) : null}
    </div>
  );
}

export const runDetailTabs: RunDetailTab[] = [
  {
    id: "changes",
    label: "Changes",
    order: 50,
    count: (d) => {
      const attempts = new Map(d.attempts.map((a) => [a.id, a.attempt_number]));
      const latest = Math.max(
        0,
        ...d.file_changes.map((c) => attempts.get(c.attempt_id ?? "") ?? 0),
      );
      return d.file_changes.filter((c) => (attempts.get(c.attempt_id ?? "") ?? 0) === latest)
        .length;
    },
    render: ({ detail, live }) => <ChangesTab detail={detail} live={live} />,
  },
];
