/**
 * Typed client for workflows, evaluation definitions/suites, and evaluation actions.
 * Mirrors backend/src/agenttape/application/schemas/{workflows,evaluation_catalog}.py.
 */
import { keepPreviousData, useMutation, useQuery } from "@tanstack/react-query";

import { api } from "@/api/client";
import { queryKeys, useInvalidate } from "@/api/hooks";
import type { EvaluationResult, Page, RunCreateResult } from "@/api/types";

// --------------------------------------------------------------------------- definitions

export type NodeType =
  | "agent_run"
  | "command"
  | "transform"
  | "evaluation_gate"
  | "finding_review"
  | "approval"
  | "condition"
  | "collect";

export const NODE_TYPES: NodeType[] = [
  "command",
  "agent_run",
  "transform",
  "evaluation_gate",
  "finding_review",
  "approval",
  "condition",
  "collect",
];

export interface EdgeCondition {
  on?: "success" | "failure" | "always";
  branch?: boolean;
  when?: string;
}

export interface WorkflowEdge {
  from: string;
  to: string;
  condition?: EdgeCondition;
}

export interface WorkflowNode {
  key: string;
  type: NodeType;
  name?: string;
  description?: string;
  config: Record<string, unknown>;
  inputs?: Record<string, unknown>;
  input_schema?: Record<string, unknown>;
  output_schema?: Record<string, unknown>;
  timeout_seconds?: number;
  max_attempts?: number;
  retry_backoff_seconds?: number;
  continue_on_failure?: boolean;
  required?: boolean;
  position?: { x: number; y: number };
}

export interface WorkflowInput {
  name: string;
  type?: string;
  label?: string;
  required?: boolean;
  default?: unknown;
  help?: string;
  options?: string[];
}

export interface WorkflowDefinition {
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  inputs: WorkflowInput[];
  settings: { max_parallel?: number; project_id?: string | null; [k: string]: unknown };
}

export interface WorkflowVersion {
  id: string;
  workflow_id: string;
  version: number;
  definition: WorkflowDefinition;
  input_schema: Record<string, unknown> | null;
  content_hash: string;
  notes: string | null;
  created_by: string | null;
  created_at: string;
  locked_at: string | null;
}

export interface Workflow {
  id: string;
  workspace_id: string;
  project_id: string | null;
  name: string;
  description: string | null;
  tags: string[];
  archived: boolean;
  latest_version: number;
  max_concurrency: number | null;
  is_demo: boolean;
  created_at: string;
  updated_at: string;
  latest: WorkflowVersion | null;
  node_count: number | null;
  last_run_status: string | null;
  last_run_id: string | null;
  last_run_at: string | null;
}

export interface ValidationIssue {
  path: string;
  message: string;
  severity: string;
}

export interface WorkflowValidation {
  valid: boolean;
  issues: ValidationIssue[];
  order: string[] | null;
}

// --------------------------------------------------------------------------- evaluations

export interface EvaluatorTypeInfo {
  type: string;
  version: string;
  display_name: string;
  description: string;
  deterministic: boolean;
  config_schema: Record<string, unknown>;
  available: boolean;
  unavailable_reason: string | null;
}

export interface EvaluationDefinitionVersion {
  id: string;
  definition_id: string;
  version: number;
  config: Record<string, unknown>;
  required: boolean;
  content_hash: string;
  created_at: string;
  locked_at: string | null;
}

export interface EvaluationDefinition {
  id: string;
  name: string;
  description: string | null;
  evaluator_type: string;
  archived: boolean;
  latest_version: number;
  is_demo: boolean;
  created_at: string;
  updated_at: string;
  latest: EvaluationDefinitionVersion | null;
  deterministic: boolean | null;
}

export interface EvaluationSuite {
  id: string;
  name: string;
  description: string | null;
  members: { definition_id: string; version: number | null; required: boolean | null }[];
  archived: boolean;
  is_demo: boolean;
  created_at: string;
  updated_at: string;
}

export interface EvaluationRunResponse {
  results: EvaluationResult[];
  skipped: { id: string; name: string; reason: string }[];
}

// --------------------------------------------------------------------------- hooks

export const wfKeys = {
  list: (params: Record<string, unknown>) => ["workflows", params] as const,
  root: ["workflows"] as const,
  oneRoot: ["workflow"] as const,
  one: (id: string) => ["workflow", id] as const,
  versions: (id: string) => ["workflow", id, "versions"] as const,
  evaluatorTypes: ["evaluator-types"] as const,
  definitions: ["evaluation-definitions"] as const,
  suites: ["evaluation-suites"] as const,
};

export function useWorkflows(params: { q?: string; offset?: number; limit?: number }) {
  return useQuery({
    queryKey: wfKeys.list(params),
    queryFn: ({ signal }) => api<Page<Workflow>>("/workflows", { query: { ...params }, signal }),
    placeholderData: keepPreviousData,
  });
}

export function useWorkflow(id: string | undefined) {
  return useQuery({
    queryKey: wfKeys.one(id ?? ""),
    queryFn: ({ signal }) =>
      api<Workflow>(`/workflows/${encodeURIComponent(id ?? "")}`, { signal }),
    enabled: Boolean(id),
  });
}

export function useWorkflowVersions(id: string | undefined) {
  return useQuery({
    queryKey: wfKeys.versions(id ?? ""),
    queryFn: ({ signal }) =>
      api<WorkflowVersion[]>(`/workflows/${encodeURIComponent(id ?? "")}/versions`, { signal }),
    enabled: Boolean(id),
  });
}

export function validateDefinition(definition: WorkflowDefinition) {
  return api<WorkflowValidation>("/workflows/validate", {
    method: "POST",
    body: { definition },
  });
}

function useInvalidateWorkflows() {
  return useInvalidate(wfKeys.root, wfKeys.oneRoot);
}

export function useCreateWorkflow() {
  const invalidate = useInvalidateWorkflows();
  return useMutation({
    mutationFn: (body: {
      name: string;
      description?: string;
      definition: WorkflowDefinition;
      notes?: string;
    }) => api<Workflow>("/workflows", { method: "POST", body }),
    onSuccess: invalidate,
  });
}

export function useUpdateWorkflow(workflowId: string) {
  const invalidate = useInvalidateWorkflows();
  return useMutation({
    mutationFn: (body: { name?: string; description?: string; archived?: boolean }) =>
      api<Workflow>(`/workflows/${encodeURIComponent(workflowId)}`, { method: "PATCH", body }),
    onSuccess: invalidate,
  });
}

export function useSaveVersion(workflowId: string) {
  const invalidate = useInvalidateWorkflows();
  return useMutation({
    mutationFn: (body: { definition: WorkflowDefinition; notes?: string }) =>
      api<WorkflowVersion>(`/workflows/${encodeURIComponent(workflowId)}/versions`, {
        method: "POST",
        body,
      }),
    onSuccess: invalidate,
  });
}

export function useStartWorkflowRun(workflowId: string) {
  const invalidate = useInvalidate(queryKeys.runsRoot, wfKeys.root);
  return useMutation({
    mutationFn: (body: { inputs: Record<string, unknown>; version_id?: string }) =>
      api<RunCreateResult>(`/workflows/${encodeURIComponent(workflowId)}/runs`, {
        method: "POST",
        body,
      }),
    onSuccess: invalidate,
  });
}

export function useEvaluatorTypes() {
  return useQuery({
    queryKey: wfKeys.evaluatorTypes,
    queryFn: () => api<EvaluatorTypeInfo[]>("/evaluator-types"),
    staleTime: 60_000,
  });
}

export function useEvaluationDefinitions() {
  return useQuery({
    queryKey: wfKeys.definitions,
    queryFn: () =>
      api<Page<EvaluationDefinition>>("/evaluation-definitions", { query: { limit: 200 } }),
  });
}

export function useEvaluationSuites() {
  return useQuery({
    queryKey: wfKeys.suites,
    queryFn: () => api<Page<EvaluationSuite>>("/evaluation-suites", { query: { limit: 200 } }),
  });
}

export function useCreateDefinition() {
  const invalidate = useInvalidate(wfKeys.definitions);
  return useMutation({
    mutationFn: (body: {
      name: string;
      description?: string;
      evaluator_type: string;
      config: Record<string, unknown>;
      required: boolean;
    }) => api<EvaluationDefinition>("/evaluation-definitions", { method: "POST", body }),
    onSuccess: invalidate,
  });
}

export function useCreateDefinitionVersion(definitionId: string) {
  const invalidate = useInvalidate(wfKeys.definitions);
  return useMutation({
    mutationFn: (body: { config: Record<string, unknown>; required: boolean }) =>
      api<EvaluationDefinitionVersion>(
        `/evaluation-definitions/${encodeURIComponent(definitionId)}/versions`,
        { method: "POST", body },
      ),
    onSuccess: invalidate,
  });
}

export function useCreateSuite() {
  const invalidate = useInvalidate(wfKeys.suites);
  return useMutation({
    mutationFn: (body: {
      name: string;
      description?: string;
      members: { definition_id: string }[];
    }) => api<EvaluationSuite>("/evaluation-suites", { method: "POST", body }),
    onSuccess: invalidate,
  });
}

export function useRerunEvaluations(runId: string) {
  const invalidate = useInvalidate(queryKeys.run(runId));
  return useMutation({
    mutationFn: () =>
      api<EvaluationRunResponse>(`/runs/${encodeURIComponent(runId)}/evaluations/rerun`, {
        method: "POST",
        body: {},
      }),
    onSuccess: invalidate,
  });
}

export function useEvaluateRun(runId: string) {
  const invalidate = useInvalidate(queryKeys.run(runId));
  return useMutation({
    mutationFn: (body: { suite_id?: string; definition_ids?: string[] }) =>
      api<EvaluationRunResponse>(`/runs/${encodeURIComponent(runId)}/evaluations/run`, {
        method: "POST",
        body,
      }),
    onSuccess: invalidate,
  });
}

export function useHumanScore(runId: string) {
  const invalidate = useInvalidate(queryKeys.run(runId));
  return useMutation({
    mutationFn: (body: {
      name: string;
      score: number;
      max_score: number;
      verdict: "pass" | "fail" | "warning";
      notes?: string;
    }) =>
      api<EvaluationResult>(`/runs/${encodeURIComponent(runId)}/evaluations/human`, {
        method: "POST",
        body,
      }),
    onSuccess: invalidate,
  });
}
