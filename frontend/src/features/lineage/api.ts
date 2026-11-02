/** Types and hooks for replay, fork, lineage, comparison, and retention. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/api/client";
import { queryKeys, useInvalidate } from "@/api/hooks";
import type { Run, RunCreateResult, RunDetail, RunStatus, Usage } from "@/api/types";

/** Compare URL for a run: against its parent, else its first child, else pick on the page. */
export function compareTarget(detail: RunDetail): string {
  const { run } = detail;
  if (run.parent_run_id) return `/runs/compare?a=${run.parent_run_id}&b=${run.id}`;
  const child = detail.children[0];
  if (child) return `/runs/compare?a=${run.id}&b=${child.id}`;
  return `/runs/compare?a=${run.id}`;
}

export interface DriftItem {
  field: string;
  label: string;
  original: unknown;
  current: unknown;
  message: string;
}

export interface LineageRun {
  id: string;
  title: string;
  status: RunStatus;
  source: string;
  relation: string | null;
  created_at: string;
}

export interface VersionChoice {
  id: string;
  version: number;
  label: string;
  created_at: string | null;
}

export interface VariableDef {
  name: string;
  type?: string;
  label?: string | null;
  help?: string | null;
  options?: string[] | null;
  required?: boolean;
  default?: unknown;
}

export interface ForkOptions {
  replayable: boolean;
  forkable: boolean;
  fork_reason: string | null;
  reason: string | null;
  prompt_template_id: string | null;
  prompt_name: string | null;
  prompt_version_id: string | null;
  prompt_versions: VersionChoice[];
  variables: VariableDef[];
  inputs: Record<string, unknown>;
  agent_profile_id: string | null;
  agent_profile_version_id: string | null;
  profiles: { id: string; name: string; adapter_type: string; versions: VersionChoice[] }[];
}

export interface Lineage {
  run_id: string;
  parent: LineageRun | null;
  children: LineageRun[];
  drift: DriftItem[];
  options: ForkOptions;
  replay_of: { of_run_id: string; drift: DriftItem[] } | null;
  fork_of: {
    of_run_id: string;
    changes: { field: string; from: unknown; to: unknown }[];
    drift: DriftItem[];
  } | null;
}

export interface LineageCreateResult {
  result: RunCreateResult;
  drift: DriftItem[];
  changes: { field: string; from: unknown; to: unknown }[];
}

export interface ForkBody {
  title?: string;
  prompt_version_id?: string | null;
  agent_profile_id?: string | null;
  agent_profile_version_id?: string | null;
  inputs: Record<string, unknown>;
}

export interface RunSide {
  run: Run;
  attempts: number;
  event_count: number;
  event_types: Record<string, number>;
  usage: Usage | null;
  usage_status: "reported" | "simulated" | "unavailable";
  cost: Record<string, unknown> | null;
  evaluations: { name: string; status: string; required: boolean; evaluator_type: string }[];
  artifacts: { name: string; kind: string; size_bytes: number; sha256: string }[];
  git: {
    files: number;
    insertions: number;
    deletions: number;
    protected: number;
    binary: number;
    head: string | null;
    branch: string | null;
    dirty: boolean | null;
  };
  prompt: {
    name: string | null;
    version: number | null;
    version_id: string | null;
    content_hash: string | null;
    rendered: string | null;
  } | null;
  profile: {
    name: string | null;
    version: number | null;
    adapter_type: string | null;
    config_hash: string | null;
  } | null;
  inputs: Record<string, unknown>;
}

export interface NamedDiff {
  in_a: boolean;
  in_b: boolean;
  changed: boolean;
  [key: string]: unknown;
}

export interface Comparison {
  a: RunSide;
  b: RunSide;
  relation: string | null;
  differences: {
    status: { a: string; b: string; same: boolean };
    duration_ms: { a: number | null; b: number | null; delta: number | null };
    attempts: { a: number; b: number; delta: number };
    events: { a: number; b: number; delta: number };
    event_types: { key: string; a: number; b: number; delta: number }[];
    usage: {
      a_status: string;
      b_status: string;
      rows: { key: string; a: number | null; b: number | null; delta: number | null }[];
    };
    evaluations: NamedDiff[];
    artifacts: NamedDiff[];
    files: NamedDiff[];
    prompt: {
      same_template: boolean;
      same_version: boolean;
      same_content: boolean;
      same_rendered: boolean;
      a_version: number | null;
      b_version: number | null;
    };
    profile: {
      same_profile: boolean;
      same_version: boolean;
      a_version: number | null;
      b_version: number | null;
      config: { key: string; a: unknown; b: unknown }[];
    };
    inputs: { key: string; a: unknown; b: unknown }[];
  };
}

export const lineageKey = (runId: string) => ["run", runId, "lineage"] as const;

export function useLineage(runId: string, enabled = true) {
  return useQuery({
    queryKey: lineageKey(runId),
    queryFn: ({ signal }) => api<Lineage>(`/runs/${encodeURIComponent(runId)}/lineage`, { signal }),
    enabled,
  });
}

export function useComparison(a: string | null, b: string | null) {
  return useQuery({
    queryKey: ["compare", a, b],
    queryFn: ({ signal }) =>
      api<Comparison>(
        `/runs/${encodeURIComponent(a ?? "")}/compare/${encodeURIComponent(b ?? "")}`,
        { signal },
      ),
    enabled: Boolean(a && b && a !== b),
  });
}

export function useReplay(runId: string) {
  const invalidate = useInvalidate(queryKeys.runsRoot, queryKeys.run(runId));
  return useMutation({
    mutationFn: () =>
      api<LineageCreateResult>(`/runs/${encodeURIComponent(runId)}/replay`, {
        method: "POST",
        body: {},
      }),
    onSuccess: invalidate,
  });
}

export function useFork(runId: string) {
  const invalidate = useInvalidate(queryKeys.runsRoot, queryKeys.run(runId));
  return useMutation({
    mutationFn: (body: ForkBody) =>
      api<LineageCreateResult>(`/runs/${encodeURIComponent(runId)}/fork`, {
        method: "POST",
        body,
      }),
    onSuccess: invalidate,
  });
}

// --------------------------------------------------------------------------- retention

export interface RetentionSettings {
  max_age_days: number;
  statuses: string[];
  include_demo: boolean;
  keep_tag: string;
  remove_worktrees: boolean;
  include_orphans: boolean;
}

export interface RetentionPreview {
  settings: RetentionSettings;
  cutoff: string;
  generated_at: string;
  runs: {
    run_id: string;
    title: string;
    status: RunStatus;
    finished_at: string | null;
    is_demo: boolean;
    artifact_bytes: number;
    log_bytes: number;
    worktree_bytes: number;
    total_bytes: number;
    worktree_path: string | null;
    blocked: string | null;
  }[];
  orphans: { kind: string; path: string; bytes: number; blocked: string | null }[];
  total_runs: number;
  total_bytes: number;
  blocked_runs: number;
}

export interface RetentionResult {
  deleted_runs: string[];
  skipped: { run_id?: string; path?: string; reason: string }[];
  removed_paths: string[];
  freed_bytes: number;
}

export function useRetentionSettings() {
  return useQuery({
    queryKey: ["retention", "settings"],
    queryFn: () => api<RetentionSettings>("/retention/settings"),
  });
}

export function useSaveRetention() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: RetentionSettings) =>
      api<RetentionSettings>("/retention/settings", { method: "PUT", body }),
    onSuccess: (data) => client.setQueryData(["retention", "settings"], data),
  });
}

export function usePreviewRetention() {
  return useMutation({
    mutationFn: (body: RetentionSettings) =>
      api<RetentionPreview>("/retention/preview", { method: "POST", body }),
  });
}

export function useExecuteRetention() {
  const invalidate = useInvalidate(queryKeys.runsRoot);
  return useMutation({
    mutationFn: (body: { run_ids: string[]; orphan_paths: string[] }) =>
      api<RetentionResult>("/retention/execute", { method: "POST", body }),
    onSuccess: invalidate,
  });
}
