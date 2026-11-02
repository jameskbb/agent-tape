import type { RunDetailTab } from "@/app/registry";

import { EvaluationsPanel } from "./EvaluationsPanel";
import { WorkflowRunTab } from "./WorkflowRunTab";

/**
 * "Workflow" shows the DAG with live node statuses for workflow runs. "Evaluations"
 * replaces the core tab (same id; this folder sorts after `runs`) with one that can rerun
 * deterministic evaluations, run a suite, and record human scores.
 */
export const runDetailTabs: RunDetailTab[] = [
  {
    id: "workflow",
    label: "Workflow",
    order: 20,
    isAvailable: (detail) => detail.run.kind === "workflow",
    count: (detail) => detail.steps.length,
    render: ({ detail }) => <WorkflowRunTab detail={detail} />,
  },
  {
    id: "evaluations",
    label: "Evaluations",
    order: 70,
    count: (d) => d.evaluations.length,
    render: ({ detail }) => <EvaluationsPanel detail={detail} />,
  },
];
