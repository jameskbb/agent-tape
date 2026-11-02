import { useQuery } from "@tanstack/react-query";
import { Download, FileText } from "lucide-react";
import { useState } from "react";

import { describeError, downloadFromApi, fetchText } from "@/api/client";
import type { Artifact, RunDetail } from "@/api/types";
import type { RunDetailTab } from "@/app/registry";
import {
  Badge,
  Button,
  Card,
  CodeBlock,
  DataTable,
  Duration,
  EmptyState,
  ErrorState,
  JsonInspector,
  LoadingBlock,
  RelativeTime,
  StatusPill,
  Unavailable,
  useToast,
  type Column,
} from "@/components/ui";
import { formatBytes, humanize, text } from "@/lib/format";

import { Timeline } from "./timeline/Timeline";

function AttemptsTab({ detail }: { detail: RunDetail }) {
  const toast = useToast();
  const columns: Column<RunDetail["attempts"][number]>[] = [
    {
      id: "n",
      header: "Attempt",
      cell: (a) => <span className="font-medium">#{a.attempt_number}</span>,
    },
    { id: "status", header: "Status", cell: (a) => <StatusPill status={a.status} size="sm" /> },
    { id: "started", header: "Started", cell: (a) => <RelativeTime iso={a.started_at} /> },
    {
      id: "duration",
      header: "Duration",
      align: "right",
      cell: (a) =>
        a.duration_ms !== null ? (
          <Duration ms={a.duration_ms} />
        ) : (
          <Unavailable reason="Still running" />
        ),
    },
    {
      id: "exit",
      header: "Exit code",
      align: "right",
      cell: (a) =>
        a.exit_code !== null ? <code>{a.exit_code}</code> : <Unavailable reason="Not reported" />,
    },
    {
      id: "failure",
      header: "Outcome",
      cell: (a) =>
        a.failure_category ? (
          <span className="text-sm">
            <span className="font-medium text-[var(--st-failed)]">
              {humanize(a.failure_category)}
            </span>
            {a.failure_message ? (
              <span className="block text-xs text-muted">{a.failure_message}</span>
            ) : null}
            {a.retry_scheduled_at ? (
              <span className="block text-xs text-muted">
                Retry scheduled <RelativeTime iso={a.retry_scheduled_at} />
              </span>
            ) : null}
          </span>
        ) : a.has_checkpoint && a.status === "waiting_for_approval" ? (
          <span className="text-sm text-muted">Paused with a checkpoint</span>
        ) : (
          <span className="text-sm text-muted">
            {a.status === "succeeded" ? "Completed" : "In progress"}
          </span>
        ),
    },
    {
      id: "logs",
      header: "Raw logs",
      cell: (a) => (
        <div className="flex gap-1">
          {(["stdout", "stderr"] as const).map((stream) => {
            const bytes = stream === "stdout" ? a.stdout_bytes : a.stderr_bytes;
            return (
              <Button
                key={stream}
                size="sm"
                variant="ghost"
                disabled={bytes === 0}
                title={
                  bytes === 0 ? "Nothing was written" : `Download ${stream} (${formatBytes(bytes)})`
                }
                icon={<Download className="size-3.5" aria-hidden />}
                onClick={() =>
                  void downloadFromApi(
                    `/runs/${detail.run.id}/logs/${a.attempt_number}/${stream}`,
                    `${detail.run.id}-attempt-${a.attempt_number}-${stream}.log`,
                  ).catch((e: unknown) => toast.show(describeError(e), "error"))
                }
              >
                {stream}
              </Button>
            );
          })}
        </div>
      ),
    },
  ];
  if (detail.attempts.length === 0)
    return (
      <EmptyState
        title="No attempts yet"
        description="An attempt starts when a worker picks up this run."
      />
    );
  return (
    <DataTable caption="Attempts" columns={columns} rows={detail.attempts} rowKey={(a) => a.id} />
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <Card title={title}>
      <div className="flex flex-col gap-3">{children}</div>
    </Card>
  );
}

function InputsTab({ detail }: { detail: RunDetail }) {
  const snap = detail.snapshot as Record<string, Record<string, unknown> | undefined>;
  const prompt = snap.prompt;
  const profile = snap.agent_profile;
  const project = snap.project;
  const git = snap.git;
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Section title="Inputs">
        {Object.keys(detail.inputs).length ? (
          <JsonInspector value={detail.inputs} defaultDepth={2} />
        ) : (
          <p className="text-sm text-muted">This run has no input variables.</p>
        )}
      </Section>
      <Section title="Prompt as sent">
        {detail.rendered_prompt !== null ? (
          <>
            <CodeBlock code={detail.rendered_prompt} maxHeight="18rem" />
            <p className="text-xs text-muted">
              Secret references appear as [secret:NAME]. Their values are resolved only when the
              agent starts and are never stored.
            </p>
          </>
        ) : (
          <p className="text-sm text-muted">No prompt was used.</p>
        )}
        {prompt ? (
          <p className="text-sm text-muted">
            {prompt.name ? (
              <>
                Template <span className="text-ink">{text(prompt.name)}</span>, version{" "}
                {text(prompt.version)}.
              </>
            ) : (
              "Inline prompt."
            )}
          </p>
        ) : null}
      </Section>
      {profile ? (
        <Section title="Agent profile snapshot">
          <p className="text-sm text-muted">
            <span className="text-ink">{text(profile.name)}</span>, version {text(profile.version)},
            adapter {text(profile.adapter_type)}.
          </p>
          <JsonInspector label="Configuration" value={profile.config ?? {}} defaultDepth={2} />
        </Section>
      ) : null}
      {project ? (
        <Section title="Project and repository">
          <p className="text-sm">
            <span className="text-ink">{text(project.name)}</span> at{" "}
            <code>{text(project.repo_path)}</code>
          </p>
          {git ? (
            git.is_repo ? (
              <p className="text-sm text-muted">
                Branch <code>{text(git.branch, "detached")}</code>, HEAD{" "}
                <code>{text(git.head, "none").slice(0, 12)}</code>,{" "}
                {git.dirty ? (
                  <span className="text-[var(--st-cancelling)]">
                    {text(git.dirty_count)} uncommitted changes when the run started
                  </span>
                ) : (
                  "clean working tree"
                )}
                .
              </p>
            ) : (
              <p className="text-sm text-muted">Not a Git repository.</p>
            )
          ) : null}
        </Section>
      ) : null}
      <div className="lg:col-span-2">
        <Section title="Full immutable snapshot">
          <p className="text-sm text-muted">
            Everything that determined this run, captured when it was created. Later edits to
            prompts, profiles, or projects never change it.
          </p>
          <JsonInspector value={detail.snapshot} defaultDepth={1} />
        </Section>
      </div>
    </div>
  );
}

export function ArtifactViewer({ artifact }: { artifact: Artifact }) {
  const content = useQuery({
    queryKey: ["artifact-content", artifact.id],
    queryFn: () => fetchText(`/artifacts/${artifact.id}/content`),
    enabled: artifact.is_text && artifact.size_bytes <= 2 * 1024 * 1024,
  });
  if (!artifact.is_text)
    return <p className="text-sm text-muted">Binary artifact; download it to inspect.</p>;
  if (artifact.size_bytes > 2 * 1024 * 1024)
    return <p className="text-sm text-muted">Too large to preview; download it instead.</p>;
  if (content.isPending) return <LoadingBlock />;
  if (content.isError)
    return <ErrorState error={content.error} onRetry={() => void content.refetch()} />;
  return <CodeBlock code={content.data} label={artifact.name} maxHeight="28rem" />;
}

/** The run's own structured output (``run.outputs``), as reported by its adapter or workflow. */
export function RunOutputCard({ detail }: { detail: RunDetail }) {
  const outputs = detail.outputs;
  const empty = !outputs || Object.keys(outputs).length === 0;
  return (
    <Card
      title="Run output"
      description="Structured output reported by the agent or workflow when the run ended."
    >
      {empty ? (
        <p className="text-sm text-muted">
          {detail.run.finished_at
            ? "This run did not report structured output. Its files and reports are listed below."
            : "Output appears here when the run finishes."}
        </p>
      ) : (
        <JsonInspector value={outputs} label="outputs" defaultDepth={2} />
      )}
    </Card>
  );
}

function OutputAndArtifactsTab({ detail }: { detail: RunDetail }) {
  return (
    <div className="flex flex-col gap-4">
      <RunOutputCard detail={detail} />
      <ArtifactsTab detail={detail} />
    </div>
  );
}

function ArtifactsTab({ detail }: { detail: RunDetail }) {
  const [selected, setSelected] = useState<string | null>(detail.artifacts[0]?.id ?? null);
  const toast = useToast();
  if (detail.artifacts.length === 0)
    return (
      <EmptyState
        title="No artifacts"
        description="Reports, diffs, and other files an agent saves appear here."
      />
    );
  const current = detail.artifacts.find((a) => a.id === selected) ?? detail.artifacts[0];
  return (
    <div className="grid gap-4 lg:grid-cols-[18rem_minmax(0,1fr)]">
      <ul className="flex flex-col gap-1" aria-label="Artifacts">
        {detail.artifacts.map((a) => (
          <li key={a.id}>
            <button
              type="button"
              aria-current={a.id === current?.id}
              onClick={() => setSelected(a.id)}
              className={`flex w-full items-start gap-2 rounded-[var(--radius-control)] border px-3 py-2 text-left ${a.id === current?.id ? "border-accent bg-accent-soft" : "border-line bg-surface hover:bg-sunken"}`}
            >
              <FileText aria-hidden className="mt-0.5 size-4 shrink-0 text-muted" />
              <span className="min-w-0">
                <span className="block truncate font-medium">{a.name}</span>
                <span className="block text-xs text-muted">
                  {humanize(a.kind)}, {formatBytes(a.size_bytes)}
                </span>
              </span>
            </button>
          </li>
        ))}
      </ul>
      {current ? (
        <div className="flex min-w-0 flex-col gap-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-sm text-muted">
              SHA-256 <code title={current.sha256}>{current.sha256.slice(0, 16)}</code>,{" "}
              {current.media_type}
            </p>
            <Button
              size="sm"
              icon={<Download className="size-3.5" aria-hidden />}
              onClick={() =>
                void downloadFromApi(
                  `/artifacts/${current.id}/content?download=true`,
                  current.name,
                ).catch((e: unknown) => toast.show(describeError(e), "error"))
              }
            >
              Download
            </Button>
          </div>
          <ArtifactViewer artifact={current} />
        </div>
      ) : null}
    </div>
  );
}

const EVAL_TONE = {
  pass: "success",
  fail: "danger",
  warning: "warning",
  error: "danger",
  skipped: "neutral",
} as const;

function EvaluationsTab({ detail }: { detail: RunDetail }) {
  if (detail.evaluations.length === 0) {
    return (
      <EmptyState
        title="No evaluations ran"
        description="Add evaluators to the agent profile (for example an exit code check) to grade every attempt."
      />
    );
  }
  return (
    <ul className="flex flex-col gap-3">
      {detail.evaluations.map((e) => (
        <li key={e.id} className="rounded-[var(--radius-panel)] border border-line bg-surface p-4">
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={EVAL_TONE[e.status]}>{humanize(e.status)}</Badge>
            <span className="font-medium">{e.name}</span>
            {e.required ? <Badge tone="accent">Required gate</Badge> : null}
            <span className="ml-auto text-xs text-muted">
              {e.evaluator_type} v{e.evaluator_version}
              {e.duration_ms !== null ? `, ${e.duration_ms} ms` : ""}
            </span>
          </div>
          {e.explanation ? <p className="mt-2 text-sm text-ink-soft">{e.explanation}</p> : null}
          <div className="mt-3">
            <JsonInspector label="Evidence" value={e.evidence} defaultDepth={2} />
          </div>
        </li>
      ))}
    </ul>
  );
}

export const runDetailTabs: RunDetailTab[] = [
  {
    id: "timeline",
    label: "Timeline",
    order: 10,
    count: (d) => d.event_count,
    render: ({ detail, events, eventsLoading, live }) => (
      <Timeline detail={detail} events={events} loading={eventsLoading} live={live} />
    ),
  },
  {
    id: "attempts",
    label: "Attempts",
    order: 30,
    count: (d) => d.attempts.length,
    render: ({ detail }) => <AttemptsTab detail={detail} />,
  },
  {
    id: "inputs",
    label: "Inputs and snapshot",
    order: 40,
    render: ({ detail }) => <InputsTab detail={detail} />,
  },
  {
    id: "artifacts",
    label: "Output and artifacts",
    order: 60,
    count: (d) => d.artifacts.length,
    render: ({ detail }) => <OutputAndArtifactsTab detail={detail} />,
  },
  {
    id: "evaluations",
    label: "Evaluations",
    order: 70,
    count: (d) => d.evaluations.length,
    render: ({ detail }) => <EvaluationsTab detail={detail} />,
  },
];
