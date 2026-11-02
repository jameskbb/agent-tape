/** Project Context Activity: which sources runs retrieve and cite, what is stale or missing,
 * and which reviewed lessons now reach future runs. Every number links back to runs. */
import type { ReactNode } from "react";
import { Link, useSearchParams } from "react-router";

import { Page } from "@/components/layout/Page";
import { Badge, Card, EmptyState, ErrorState, LoadingBlock, Select } from "@/components/ui";
import { humanize, shortId } from "@/lib/format";
import { useProjects } from "@/features/projects/api";

import { useContextActivity, type SourceMetric } from "./api";
import { destinationLabel } from "./labels";

function RunLinks({ runs }: { runs: string[] }) {
  if (runs.length === 0) return null;
  return (
    <span className="flex flex-wrap gap-1.5 text-xs">
      {runs.map((id) => (
        <Link key={id} to={`/runs/${id}?tab=context`} className="text-accent underline">
          {shortId(id)}
        </Link>
      ))}
    </span>
  );
}

function MetricList({
  title,
  description,
  rows,
  unit,
}: {
  title: string;
  description: string;
  rows: SourceMetric[];
  unit: string;
}) {
  return (
    <Card title={title} description={description}>
      {rows.length === 0 ? (
        <p className="text-sm text-muted">Nothing recorded yet.</p>
      ) : (
        <ol className="flex flex-col gap-2 text-sm">
          {rows.map((r) => (
            <li key={r.source.id} className="flex flex-col gap-0.5">
              <span className="flex flex-wrap items-baseline justify-between gap-2">
                <span className="font-medium">{r.source.name ?? r.source.key ?? r.source.id}</span>
                <span className="text-xs text-muted tabular-nums">
                  {r.count} {unit} · {r.run_count} run{r.run_count === 1 ? "" : "s"}
                </span>
              </span>
              <RunLinks runs={r.runs} />
            </li>
          ))}
        </ol>
      )}
    </Card>
  );
}

function Section({ children }: { children: ReactNode }) {
  return <div className="grid gap-4 lg:grid-cols-2">{children}</div>;
}

function Activity({ projectId }: { projectId: string }) {
  const q = useContextActivity(projectId);
  if (q.isPending) return <LoadingBlock label="Loading context activity" />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />;
  const a = q.data;
  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm text-muted">{a.note}</p>
      <Section>
        <MetricList
          title="Most retrieved sources"
          description="Sources agents searched, opened, read, or queried."
          rows={a.most_retrieved}
          unit="retrievals"
        />
        <MetricList
          title="Most cited sources"
          description="Sources findings pointed to as evidence."
          rows={a.most_cited}
          unit="citations"
        />
        <MetricList
          title="Available but never selected or retrieved"
          description="Accessible to runs (skills, declared MCP servers) yet never placed in a pack or retrieved."
          rows={a.rarely_selected}
          unit="runs available"
        />
        <MetricList
          title="Stale or unavailable sources"
          description="Sources reported stale, or that a run tried and could not reach."
          rows={a.stale_or_unavailable}
          unit="reports"
        />
      </Section>
      <Section>
        <Card title="Context gaps and feedback by category">
          {a.gaps_by_category.length === 0 ? (
            <p className="text-sm text-muted">No gaps or corrections recorded.</p>
          ) : (
            <ul className="flex flex-col gap-2 text-sm">
              {a.gaps_by_category.map((g) => (
                <li key={g.kind} className="flex flex-col gap-0.5">
                  <span>
                    <Badge>{humanize(g.kind)}</Badge>{" "}
                    <span className="tabular-nums">{g.count}</span>
                  </span>
                  <RunLinks runs={g.runs} />
                </li>
              ))}
            </ul>
          )}
          <h3 className="mt-4 mb-1 text-sm font-semibold">
            Runs with missing context that failed or were corrected
          </h3>
          {a.runs_with_missing_context_and_failure_or_correction.length === 0 ? (
            <p className="text-sm text-muted">None.</p>
          ) : (
            <RunLinks runs={a.runs_with_missing_context_and_failure_or_correction} />
          )}
        </Card>
        <Card
          title="Prompt versions and evaluation outcomes"
          description="Evaluation results recorded for runs of each prompt version. An association only."
        >
          {a.prompt_versions.length === 0 ? (
            <p className="text-sm text-muted">No prompt-based runs in this project.</p>
          ) : (
            <ul className="flex flex-col gap-2 text-sm">
              {a.prompt_versions.map((p) => (
                <li key={`${p.prompt}-${p.version}`} className="flex flex-col gap-0.5">
                  <span className="flex flex-wrap justify-between gap-2">
                    <span className="font-medium">
                      {p.prompt}
                      {p.version != null ? ` v${p.version}` : ""}
                    </span>
                    <span className="text-xs text-muted tabular-nums">
                      {p.evaluated
                        ? `${p.passed} passed · ${p.failed} failed evaluations`
                        : "No evaluations recorded"}
                    </span>
                  </span>
                  <RunLinks runs={p.runs} />
                </li>
              ))}
            </ul>
          )}
        </Card>
      </Section>
      <Card
        title="Approved lessons"
        description={`Reviewed feedback and the run it came from. ${a.pending_feedback} item${a.pending_feedback === 1 ? "" : "s"} still pending review.`}
      >
        {a.lessons.length === 0 ? (
          <p className="text-sm text-muted">No feedback has been approved in this project yet.</p>
        ) : (
          <ul className="flex flex-col gap-3 text-sm">
            {a.lessons.map((l) => (
              <li
                key={l.id}
                className="rounded border border-line p-3"
                data-testid="approved-lesson"
              >
                <div className="flex flex-wrap items-center gap-2">
                  {l.lesson_version != null ? (
                    <Badge tone="violet">Lesson v{l.lesson_version}</Badge>
                  ) : (
                    <Badge>Not injected</Badge>
                  )}
                  <span className="text-xs text-muted">
                    {destinationLabel(l.proposed_destination)}
                  </span>
                </div>
                <p className="mt-1">{l.lesson_text ?? l.text}</p>
                <p className="mt-1 text-xs text-muted">
                  From run{" "}
                  <Link to={`/runs/${l.run_id}?tab=context`} className="text-accent underline">
                    {shortId(l.run_id)}
                  </Link>
                  , approved by {l.reviewed_by ?? "a reviewer"}
                  {l.lesson_version != null
                    ? `; injected into ${l.injected_runs ?? 0} later run${l.injected_runs === 1 ? "" : "s"}`
                    : ""}
                </p>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

export function ContextActivityPage() {
  const [params, setParams] = useSearchParams();
  const projects = useProjects({ limit: 200 });
  const items = projects.data?.items ?? [];
  const projectId = params.get("project") ?? items[0]?.id ?? null;
  return (
    <Page
      title="Context activity"
      description="Which sources a project's runs retrieved and cited, what was stale or missing, and which reviewed lessons now reach future runs."
      actions={
        <Select
          label="Project"
          aria-label="Project"
          value={projectId ?? ""}
          onChange={(v) => setParams({ project: v }, { replace: true })}
          options={items.map((p) => ({ value: p.id, label: p.name }))}
        />
      }
    >
      {projects.isPending ? (
        <LoadingBlock label="Loading projects" />
      ) : projects.isError ? (
        <ErrorState error={projects.error} onRetry={() => void projects.refetch()} />
      ) : !projectId ? (
        <EmptyState
          title="No projects yet"
          description="Context activity is collected per project. Add a project and run an agent in it."
        />
      ) : (
        <Activity projectId={projectId} />
      )}
    </Page>
  );
}
