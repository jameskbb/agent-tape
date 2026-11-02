import { Workflow } from "lucide-react";

import type { FeatureManifest } from "@/app/registry";

import { EvaluationsPage } from "./EvaluationsPage";
import { EditWorkflowPage, NewWorkflowPage } from "./WorkflowEditorPage";
import { WorkflowDetailPage } from "./WorkflowDetailPage";
import { WorkflowsPage } from "./WorkflowsPage";

export const feature: FeatureManifest = {
  id: "workflows",
  nav: { label: "Workflows", icon: Workflow, order: 30, path: "/workflows" },
  routes: [
    { path: "workflows", element: <WorkflowsPage /> },
    { path: "workflows/new", element: <NewWorkflowPage /> },
    { path: "workflows/evaluations", element: <EvaluationsPage /> },
    { path: "workflows/:workflowId", element: <WorkflowDetailPage /> },
    { path: "workflows/:workflowId/edit", element: <EditWorkflowPage /> },
  ],
};
