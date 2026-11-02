import { useState } from "react";
import { Link } from "react-router";

import type { RunDetail, RunStatus, StepRun } from "@/api/types";
import { Card, Duration, EmptyState, JsonInspector, StatusPill } from "@/components/ui";
import { humanize, shortId } from "@/lib/format";

import type { WorkflowDefinition } from "./api";
import { DagList } from "./DagList";
import { TYPE_LABELS } from "./graph";
import { WorkflowGraph } from "./WorkflowGraph";

export function workflowSnapshot(detail: RunDetail): {
  definition: WorkflowDefinition | null;
  name: string | null;
  version: number | null;
  workflowId: string | null;
} {
  const section = (detail.snapshot.workflow ?? {}) as {
    definition?: WorkflowDefinition;
    name?: string;
    version?: number;
    workflow_id?: string;
  };
  return {
    definition: section.definition ?? null,
    name: section.name ?? null,
    version: section.version ?? null,
    workflowId: section.workflow_id ?? null,
  };
}

function stepDetail(step: StepRun): string | undefined {
  if (step.failure_message && step.status !== "succeeded") return step.failure_message;
  if (step.attempt_count > 1) return `${step.attempt_count} attempts`;
  return undefined;
}

function StepPanel({ step, detail }: { step: StepRun; detail: RunDetail }) {
  const approval = detail.approvals.find((a) => a.id === step.approval_request_id);
  return (
    <Card
      title={step.name}
      description={
        <span>
          <code>{step.node_key}</code>, {TYPE_LABELS[step.node_type as keyof typeof TYPE_LABELS]}
        </span>
      }
      actions={<StatusPill status={step.status} size="sm" />}
    >
      <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1.5 text-sm">
        <dt className="text-muted">Attempts</dt>
        <dd>{step.attempt_count}</dd>
        <dt className="text-muted">Duration</dt>
        <dd>
          <Duration ms={step.duration_ms} />
        </dd>
        {step.child_run_id ? (
          <>
            <dt className="text-muted">Child run</dt>
            <dd>
              <Link className="text-accent underline" to={`/runs/${step.child_run_id}`}>
                {shortId(step.child_run_id)}
              </Link>
            </dd>
          </>
        ) : null}
        {approval ? (
          <>
            <dt className="text-muted">Approval</dt>
            <dd>
              {humanize(approval.status)}
              {approval.reviewer ? ` by ${approval.reviewer}` : ""}
              {approval.reviewer_note ? `: ${approval.reviewer_note}` : ""}
            </dd>
          </>
        ) : null}
        {step.failure_category ? (
          <>
            <dt className="text-muted">Failure</dt>
            <dd>
              {humanize(step.failure_category)}
              {step.failure_message ? `: ${step.failure_message}` : ""}
            </dd>
          </>
        ) : null}
      </dl>
      <div className="mt-3 flex flex-col gap-2">
        {step.inputs && Object.keys(step.inputs).length > 0 ? (
          <JsonInspector label="Resolved inputs" value={step.inputs} defaultDepth={1} />
        ) : null}
        {step.outputs ? (
          <JsonInspector label="Outputs" value={step.outputs} defaultDepth={1} />
        ) : (
          <p className="text-sm text-muted">No outputs yet.</p>
        )}
      </div>
    </Card>
  );
}

/** Run-detail tab for workflow runs: the DAG with live node statuses plus the step list. */
export function WorkflowRunTab({ detail }: { detail: RunDetail }) {
  const snap = workflowSnapshot(detail);
  const [selected, setSelected] = useState<string | null>(null);
  if (!snap.definition) {
    return (
      <EmptyState title="No workflow definition" description="This run has no workflow snapshot." />
    );
  }
  const steps = new Map(detail.steps.map((s) => [s.node_key, s]));
  const statuses: Record<string, RunStatus> = {};
  const details: Record<string, string> = {};
  for (const s of detail.steps) {
    statuses[s.node_key] = s.status;
    const d = stepDetail(s);
    if (d) details[s.node_key] = d;
  }
  const chosen = selected ? steps.get(selected) : undefined;
  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm text-muted">
        {snap.name ? (
          snap.workflowId ? (
            <Link className="text-accent underline" to={`/workflows/${snap.workflowId}`}>
              {snap.name}
            </Link>
          ) : (
            snap.name
          )
        ) : (
          "Workflow"
        )}
        {snap.version ? `, version ${snap.version} (immutable for this run)` : ""}. Select a node to
        see its inputs and outputs.
      </p>
      <WorkflowGraph
        definition={snap.definition}
        statuses={statuses}
        details={details}
        selected={selected}
        onSelect={setSelected}
      />
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <Card title="Steps" padded={false}>
          <DagList
            definition={snap.definition}
            statuses={statuses}
            selected={selected}
            onSelect={setSelected}
            label="Workflow steps"
            extra={(key) => {
              const s = steps.get(key);
              if (!s) return null;
              return (
                <span className="mt-0.5 flex flex-wrap gap-x-3 text-xs text-muted">
                  {s.duration_ms !== null ? (
                    <span>
                      Took <Duration ms={s.duration_ms} />
                    </span>
                  ) : null}
                  {s.child_run_id ? <span>Child run {shortId(s.child_run_id)}</span> : null}
                  {s.failure_message && s.status !== "succeeded" ? (
                    <span>{s.failure_message}</span>
                  ) : null}
                </span>
              );
            }}
          />
        </Card>
        {chosen ? (
          <StepPanel step={chosen} detail={detail} />
        ) : (
          <EmptyState
            title="Select a node"
            description="Choose a node in the graph or the list to inspect its resolved inputs, outputs, child run, and approval."
          />
        )}
      </div>
    </div>
  );
}
