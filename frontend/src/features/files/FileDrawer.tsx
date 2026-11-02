/** Side drawer with one file's evidence: why it is listed, states, event trail, diff, links. */
import { X } from "lucide-react";
import { useEffect, useRef } from "react";
import { Link } from "react-router";

import {
  Badge,
  DataTable,
  ErrorState,
  IconButton,
  LoadingBlock,
  Unavailable,
  type Column,
} from "@/components/ui";
import type { ChangedFile } from "@/features/changes/api";
import { DiffViewer } from "@/features/changes/DiffViewer";
import { formatBytes, formatDateTime, humanize } from "@/lib/format";

import { useFileTimeline, type EvidenceEvent, type FileState, type FileTimeline } from "./api";
import {
  classificationTone,
  evidenceSourceLabel,
  policyWording,
  protectedLabel,
  shortHash,
} from "./helpers";

const CHANGE_TYPES = new Set(["added", "modified", "deleted", "renamed"]);

function toChangedFile(t: FileTimeline): ChangedFile | null {
  const d = t.diff;
  if (!d) return null;
  return {
    id: `diff-${t.file.id}`,
    path: d.path,
    old_path: t.file.start_path && t.file.start_path !== d.path ? t.file.start_path : null,
    change_type: (CHANGE_TYPES.has(d.change_type)
      ? d.change_type
      : "modified") as ChangedFile["change_type"],
    insertions: d.insertions,
    deletions: d.deletions,
    is_binary: d.is_binary,
    size_bytes: t.end_state.size_bytes,
    is_protected: t.file.is_protected,
    protected_patterns: t.file.protected_patterns,
    pre_existing_dirty: false,
    attempt_id: null,
    hunks: d.hunks,
    diff_available: d.diff_available,
    diff_truncated: d.diff_truncated,
    diff_omitted: d.diff_omitted ?? null,
  };
}

function StateView({ label, state }: { label: string; state: FileState }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="mt-0.5 flex flex-col gap-0.5 text-sm">
        {state.exists === null ? (
          <Unavailable reason="Not captured" />
        ) : !state.exists ? (
          <span>Did not exist</span>
        ) : (
          <>
            <code className="truncate" title={state.path ?? undefined}>
              {state.path}
            </code>
            <span>
              Hash{" "}
              {state.hash ? (
                <code title={state.hash}>{shortHash(state.hash)}</code>
              ) : (
                <Unavailable reason="Not hashed" />
              )}
            </span>
            <span>
              Size{" "}
              {state.size_bytes !== null ? (
                formatBytes(state.size_bytes)
              ) : (
                <Unavailable reason="Not recorded" />
              )}
              {state.is_binary === null ? null : state.is_binary ? ", binary" : ", text"}
            </span>
          </>
        )}
      </dd>
    </div>
  );
}

const eventColumns: Column<EvidenceEvent>[] = [
  {
    id: "ts",
    header: "Time",
    cell: (e) => (
      <time dateTime={e.ts} className="whitespace-nowrap tabular-nums">
        {formatDateTime(e.ts)}
      </time>
    ),
  },
  {
    id: "op",
    header: "Operation",
    cell: (e) => (
      <span>
        {humanize(e.operation)}
        {e.previous_path ? (
          <span className="block text-xs text-muted">from {e.previous_path}</span>
        ) : null}
      </span>
    ),
  },
  { id: "source", header: "Source", cell: (e) => evidenceSourceLabel(e.source) },
  { id: "quality", header: "Quality", cell: (e) => humanize(e.quality) },
  {
    id: "tool",
    header: "Tool or command",
    cell: (e) =>
      e.command || e.tool ? (
        <code className="line-clamp-2 break-all text-xs" title={e.command ?? e.tool ?? undefined}>
          {e.command ?? e.tool}
        </code>
      ) : (
        <span className="text-faint">none</span>
      ),
  },
  { id: "count", header: "Count", align: "right", cell: (e) => e.count },
  {
    id: "policy",
    header: "Policy",
    cell: (e) =>
      e.policy || e.protected_patterns.length ? (
        <Badge tone={e.policy?.decision === "approved" ? "success" : "warning"}>
          Protected, {policyWording(e.policy)}
        </Badge>
      ) : !e.inside_boundary ? (
        <Badge tone="danger">Outside boundary</Badge>
      ) : (
        <span className="text-faint">none</span>
      ),
  },
];

function Body({
  runId,
  t,
  stepUnavailable,
}: {
  runId: string;
  t: FileTimeline;
  stepUnavailable: boolean;
}) {
  const f = t.file;
  const diff = toChangedFile(t);
  return (
    <div className="flex flex-col gap-5">
      <section aria-label="Why AgentTape lists this file" className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone={classificationTone(f)}>{f.label}</Badge>
          {f.net_unchanged ? <Badge tone="warning">Touched, net unchanged</Badge> : null}
          {f.is_protected ? <Badge tone="warning">{protectedLabel(f)}</Badge> : null}
          {f.is_outside ? <Badge tone="danger">Outside boundary</Badge> : null}
          {f.is_generated ? <Badge>Generated</Badge> : null}
        </div>
        <h3 className="text-sm font-semibold">Why AgentTape lists this file</h3>
        <p className="text-sm text-ink-soft">{f.explanation}</p>
        {f.rename_chain.length ? (
          <p className="text-sm">
            Rename chain: <code>{[...f.rename_chain, f.file_key].join(" → ")}</code>
          </p>
        ) : null}
      </section>

      <dl className="grid gap-4 sm:grid-cols-3">
        <StateView label="Start state" state={t.start_state} />
        <StateView label="End state" state={t.end_state} />
        <div>
          <dt className="text-xs text-muted">Workflow step</dt>
          <dd className="mt-0.5 text-sm">
            {f.first_step ? (
              f.last_step && f.last_step !== f.first_step ? (
                `${f.first_step} → ${f.last_step}`
              ) : (
                f.first_step
              )
            ) : stepUnavailable ? (
              <Unavailable reason="Step attribution unavailable" />
            ) : (
              <span className="text-muted">Not a workflow step</span>
            )}
          </dd>
          <dt className="mt-2 text-xs text-muted">Agent</dt>
          <dd className="mt-0.5 text-sm">
            {f.agents.length ? f.agents.join(", ") : <Unavailable reason="Not reported" />}
          </dd>
        </div>
      </dl>

      <section aria-label="Event trail" className="flex flex-col gap-2">
        <h3 className="text-sm font-semibold">Event trail</h3>
        <DataTable
          caption="Evidence event trail"
          columns={eventColumns}
          rows={t.events}
          rowKey={(e) => e.id}
          empty="No raw evidence events are stored for this file."
        />
        {t.truncated ? (
          <p className="text-xs text-muted">
            Showing the first {t.events.length} events; the evidence bundle export has them all.
          </p>
        ) : null}
      </section>

      <section aria-label="Final diff" className="flex flex-col gap-2">
        <h3 className="text-sm font-semibold">Final diff</h3>
        {diff ? (
          <DiffViewer key={diff.id} files={[diff]} />
        ) : (
          <p className="text-sm text-muted">
            {f.net_unchanged
              ? "No final diff: the file ended with its original content."
              : f.net_status === "pending"
                ? "The final diff is computed when the attempt finishes."
                : "No final diff is recorded for this file."}
          </p>
        )}
      </section>

      <section aria-label="Related records" className="grid gap-4 sm:grid-cols-2">
        <div>
          <h3 className="text-sm font-semibold">Timeline events</h3>
          {t.linked_events.length ? (
            <ul className="mt-1 flex flex-col gap-1 text-sm">
              {t.linked_events.map((e) => (
                <li key={e.id}>
                  <Link
                    to={`/runs/${runId}?tab=timeline`}
                    className="text-accent hover:underline"
                    title={formatDateTime(e.ts)}
                  >
                    #{e.sequence} {e.summary}
                  </Link>
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-1 text-sm text-muted">No timeline events reference this file.</p>
          )}
        </div>
        <div>
          <h3 className="text-sm font-semibold">Approvals</h3>
          {t.approvals.length ? (
            <ul className="mt-1 flex flex-col gap-1 text-sm">
              {t.approvals.map((a) => (
                <li key={a.id} className="flex flex-wrap items-center gap-2">
                  <Link
                    to={a.status === "pending" ? "/approvals" : "/approvals?view=resolved"}
                    className="text-accent hover:underline"
                  >
                    {a.title}
                  </Link>
                  <Badge
                    tone={
                      a.status === "approved"
                        ? "success"
                        : a.status === "pending"
                          ? "warning"
                          : "neutral"
                    }
                  >
                    {humanize(a.status)}
                  </Badge>
                  {a.reviewer ? <span className="text-muted">by {a.reviewer}</span> : null}
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-1 text-sm text-muted">No approvals are linked to this file.</p>
          )}
        </div>
      </section>
    </div>
  );
}

export function FileDrawer({
  runId,
  path,
  live,
  stepUnavailable,
  onClose,
}: {
  runId: string;
  path: string | null;
  live: boolean;
  stepUnavailable: boolean;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const query = useFileTimeline(runId, path, live);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (path !== null && !d.open) {
      if (typeof d.showModal === "function") d.showModal();
      else d.setAttribute("open", "");
    } else if (path === null && d.open) {
      if (typeof d.close === "function") d.close();
      else d.removeAttribute("open");
    }
  }, [path]);
  return (
    <dialog
      ref={ref}
      aria-label={path ? `File evidence: ${path}` : "File evidence"}
      onClose={onClose}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
      className="m-0 ml-auto h-dvh max-h-dvh w-[min(100vw,60rem)] max-w-none border-l border-line bg-raised p-0 text-ink shadow-[var(--shadow-pop)] backdrop:bg-[rgb(10_16_24/0.45)]"
    >
      {path !== null ? (
        <div className="flex flex-col gap-4 p-5">
          <header className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="text-xs text-muted">File evidence</p>
              <h2 className="text-lg font-semibold break-all">{path}</h2>
            </div>
            <IconButton label="Close" icon={<X className="size-4" />} onClick={onClose} />
          </header>
          {query.isPending ? (
            <LoadingBlock label="Loading file evidence" />
          ) : query.isError ? (
            <ErrorState error={query.error} onRetry={() => void query.refetch()} />
          ) : (
            <Body runId={runId} t={query.data} stepUnavailable={stepUnavailable} />
          )}
        </div>
      ) : null}
    </dialog>
  );
}
