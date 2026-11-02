import { useQuery } from "@tanstack/react-query";
import { Lock, Pencil, Play } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { api, describeError } from "@/api/client";
import type { Page as PageOf, Run } from "@/api/types";
import { Page } from "@/components/layout/Page";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorState,
  LoadingBlock,
  RelativeTime,
  StatusPill,
  Tabs,
  useToast,
} from "@/components/ui";

import {
  useStartWorkflowRun,
  useWorkflow,
  useWorkflowVersions,
  type WorkflowInput,
  type WorkflowVersion,
} from "./api";
import { DagList } from "./DagList";
import { Field, inputClass, LinkButton, textareaClass } from "./ui";
import { WorkflowGraph } from "./WorkflowGraph";

function initialValue(input: WorkflowInput): string {
  if (input.default === undefined || input.default === null) return "";
  return typeof input.default === "string" ? input.default : JSON.stringify(input.default);
}

function coerce(input: WorkflowInput, raw: string): unknown {
  if (raw === "") return undefined;
  if (input.type === "number") return Number(raw);
  if (input.type === "boolean") return raw === "true";
  return raw;
}

function StartRunForm({ workflowId, version }: { workflowId: string; version: WorkflowVersion }) {
  const toast = useToast();
  const navigate = useNavigate();
  const start = useStartWorkflowRun(workflowId);
  const inputs = version.definition.inputs;
  const [values, setValues] = useState<Record<string, string | undefined>>(() =>
    Object.fromEntries(inputs.map((i) => [i.name, initialValue(i)])),
  );
  const submit = (event: { preventDefault: () => void }) => {
    event.preventDefault();
    const body: Record<string, unknown> = {};
    for (const input of inputs) {
      const v = coerce(input, values[input.name] ?? "");
      if (v !== undefined) body[input.name] = v;
    }
    start.mutate(
      { inputs: body, version_id: version.id },
      {
        onSuccess: (r) => {
          toast.show("Workflow run started", "success");
          void navigate(`/runs/${r.run.id}?tab=workflow`);
        },
        onError: (e) => toast.show(describeError(e), "error"),
      },
    );
  };
  return (
    <form onSubmit={submit} className="flex flex-col gap-3" aria-label="Start a workflow run">
      {inputs.length === 0 ? (
        <p className="text-sm text-muted">This workflow takes no inputs.</p>
      ) : (
        inputs.map((input) => (
          <Field key={input.name} label={input.label || input.name} help={input.help}>
            {(props) =>
              input.type === "multiline" ? (
                <textarea
                  {...props}
                  rows={3}
                  className={textareaClass}
                  value={values[input.name] ?? ""}
                  onChange={(e) => setValues((v) => ({ ...v, [input.name]: e.target.value }))}
                />
              ) : input.type === "enum" || input.type === "boolean" ? (
                <select
                  {...props}
                  className={inputClass}
                  value={values[input.name] ?? ""}
                  onChange={(e) => setValues((v) => ({ ...v, [input.name]: e.target.value }))}
                >
                  {(input.type === "boolean" ? ["true", "false"] : (input.options ?? [])).map(
                    (o) => (
                      <option key={o} value={o}>
                        {o}
                      </option>
                    ),
                  )}
                </select>
              ) : (
                <input
                  {...props}
                  type={input.type === "number" ? "number" : "text"}
                  className={inputClass}
                  value={values[input.name] ?? ""}
                  onChange={(e) => setValues((v) => ({ ...v, [input.name]: e.target.value }))}
                />
              )
            }
          </Field>
        ))
      )}
      <div>
        <Button
          type="submit"
          variant="primary"
          loading={start.isPending}
          icon={<Play aria-hidden className="size-4" />}
        >
          Start run of v{version.version}
        </Button>
      </div>
    </form>
  );
}

function RecentRuns({ workflowId }: { workflowId: string }) {
  const runs = useQuery({
    queryKey: ["runs", { workflow_id: workflowId, kind: "workflow" }],
    queryFn: () =>
      api<PageOf<Run>>("/runs", {
        query: { workflow_id: workflowId, kind: "workflow", limit: 10 },
      }),
    refetchInterval: 5000,
  });
  if (runs.isPending) return <LoadingBlock />;
  if (runs.isError) return <ErrorState error={runs.error} onRetry={() => void runs.refetch()} />;
  if (runs.data.items.length === 0)
    return <p className="text-sm text-muted">No runs yet. Start one to see it here.</p>;
  return (
    <ul className="flex flex-col divide-y divide-line" aria-label="Recent runs of this workflow">
      {runs.data.items.map((r) => (
        <li key={r.id} className="flex items-center gap-3 py-2">
          <StatusPill status={r.status} size="sm" />
          <Link
            to={`/runs/${r.id}?tab=workflow`}
            className="min-w-0 flex-1 truncate hover:underline"
          >
            {r.title}
          </Link>
          <span className="text-sm text-muted">
            <RelativeTime iso={r.created_at} />
          </span>
        </li>
      ))}
    </ul>
  );
}

export function WorkflowDetailPage() {
  const { workflowId = "" } = useParams();
  const workflow = useWorkflow(workflowId);
  const versions = useWorkflowVersions(workflowId);
  const [view, setView] = useState("graph");
  const [versionId, setVersionId] = useState<string | null>(null);

  if (workflow.isPending || versions.isPending) {
    return (
      <Page title="Workflow">
        <LoadingBlock />
      </Page>
    );
  }
  if (workflow.isError) {
    return (
      <Page title="Workflow">
        <ErrorState
          error={workflow.error}
          resource="workflow"
          backTo="/workflows"
          backLabel="Back to workflows"
          onRetry={() => void workflow.refetch()}
        />
      </Page>
    );
  }
  const wf = workflow.data;
  const all = versions.data ?? [];
  const shown = all.find((v) => v.id === versionId) ?? wf.latest ?? all[0];

  return (
    <Page
      title={wf.name}
      breadcrumb={
        <Link to="/workflows" className="hover:underline">
          Workflows
        </Link>
      }
      description={wf.description}
      actions={
        <LinkButton
          to={`/workflows/${wf.id}/edit`}
          icon={<Pencil aria-hidden className="size-4" />}
        >
          Edit and save a new version
        </LinkButton>
      }
    >
      {!shown ? (
        <EmptyState title="No versions" description="Save a version in the editor to run it." />
      ) : (
        <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_22rem]">
          <div className="flex min-w-0 flex-col gap-4">
            <Tabs
              label="Workflow views"
              value={view}
              onChange={setView}
              items={[
                { id: "graph", label: "Graph" },
                { id: "list", label: "List", count: shown.definition.nodes.length },
              ]}
            >
              {view === "graph" ? (
                <WorkflowGraph definition={shown.definition} height={420} />
              ) : (
                <Card padded={false}>
                  <DagList definition={shown.definition} />
                </Card>
              )}
            </Tabs>
            <Card title="Recent runs">
              <RecentRuns workflowId={wf.id} />
            </Card>
          </div>
          <div className="flex flex-col gap-4">
            <Card
              title="Start a run"
              description="Runs use this exact version; later edits never change them."
            >
              <StartRunForm key={shown.id} workflowId={wf.id} version={shown} />
            </Card>
            <Card title="Versions" padded={false}>
              <ul className="flex flex-col divide-y divide-line" aria-label="Versions">
                {all.map((v) => (
                  <li key={v.id}>
                    <button
                      type="button"
                      onClick={() => setVersionId(v.id)}
                      aria-current={v.id === shown.id ? "true" : undefined}
                      className="flex w-full items-center gap-2 px-4 py-2.5 text-left hover:bg-sunken aria-[current=true]:bg-accent-soft"
                    >
                      <span className="font-medium">v{v.version}</span>
                      {v.id === wf.latest?.id ? <Badge tone="accent">Latest</Badge> : null}
                      {v.locked_at ? (
                        <Badge title="Used by a run; this version can no longer change">
                          <Lock aria-hidden className="size-3" /> Used by runs
                        </Badge>
                      ) : null}
                      <span className="ml-auto text-xs text-muted">
                        <RelativeTime iso={v.created_at} />
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            </Card>
          </div>
        </div>
      )}
    </Page>
  );
}
