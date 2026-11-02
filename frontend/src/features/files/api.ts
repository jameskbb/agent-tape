/**
 * Types and hooks for File Evidence: a run's file ledger (`/runs/{id}/files...`) and a
 * project's cross-run file activity (`/projects/{id}/file-activity...`).
 */
import { keepPreviousData, useQuery } from "@tanstack/react-query";

import { api } from "@/api/client";
import type { Page } from "@/api/types";

import type { DiffHunk } from "@/features/changes/api";

export interface FilePolicy {
  kind?: string;
  /** "approved" or "detected" (detected paths are never prevented). */
  decision?: string;
  prevented?: boolean;
  /** True when the project's policy stopped the attempt after the detection. */
  cancelled?: boolean;
  approval_id?: string | null;
  patterns?: string[];
  detected_by?: string;
  [key: string]: unknown;
}

export interface RunFile {
  id: string;
  run_id: string;
  file_key: string;
  start_path: string | null;
  end_path: string | null;
  rename_chain: string[];
  first_at: string | null;
  last_at: string | null;
  operation_counts: Record<string, number>;
  operation_total: number;
  first_step: string | null;
  last_step: string | null;
  attempts: string[];
  agents: string[];
  tools: string[];
  sources: string[];
  classification: string;
  label: string;
  classifications: string[];
  start_hash: string | null;
  end_hash: string | null;
  size_bytes: number | null;
  is_binary: boolean | null;
  net_status: string;
  insertions: number | null;
  deletions: number | null;
  net_unchanged: boolean;
  is_protected: boolean;
  protected_patterns: string[];
  is_generated: boolean;
  is_outside: boolean;
  is_collapsed: boolean;
  collapsed_count: number | null;
  completeness: string;
  explanation: string;
  policy: FilePolicy | null;
}

export interface Completeness {
  level: "complete" | "partial" | "unavailable" | "pending";
  statement: string;
  writes: string;
  reads: string;
  git: string;
  step_attribution: string;
  offline_windows: Record<string, unknown>[];
  dropped_watcher_events: number;
}

export interface FileCounts {
  files: number;
  read_or_referenced: number | null;
  created: number | null;
  modified: number | null;
  deleted: number | null;
  renamed: number | null;
  restored_or_net_unchanged: number | null;
  generated: number | null;
  protected_findings: number;
  outside_findings: number;
  collapsed_events: number;
  collapsed_directories: number;
}

/** A protected-path or outside-boundary finding in the file ledger (unrelated to the
 * Findings feature's `Finding` type: `features/findings/api.ts` owns that one). */
export interface FileFinding {
  file_key: string;
  classification: string;
  net_status: string;
  patterns: string[];
  policy: FilePolicy | null;
}

export interface FileEvidenceSummary {
  run_id: string;
  run_status: string;
  completeness: Completeness;
  counts: FileCounts;
  unavailable: Record<string, string>;
  collapsed: { path: string; count: number; by_operation: Record<string, number> }[];
  protected: FileFinding[];
  outside: FileFinding[];
  facets: Partial<
    Record<"classification" | "net_status" | "source" | "operation" | "step" | "agent", string[]>
  >;
}

export interface EvidenceEvent {
  id: string;
  sequence: number;
  ts: string;
  last_ts: string | null;
  operation: string;
  path: string;
  previous_path: string | null;
  inside_boundary: boolean;
  source: string;
  quality: string;
  adapter: string | null;
  tool: string | null;
  command: string | null;
  node_key: string | null;
  before_hash: string | null;
  after_hash: string | null;
  size_bytes: number | null;
  count: number;
  protected_patterns: string[];
  policy: FilePolicy | null;
  redacted: boolean;
}

export interface FileState {
  path: string | null;
  hash: string | null;
  size_bytes: number | null;
  is_binary: boolean | null;
  exists: boolean | null;
}

export interface FileDiff {
  path: string;
  change_type: string;
  insertions: number | null;
  deletions: number | null;
  is_binary: boolean;
  hunks: DiffHunk[];
  diff_available: boolean;
  diff_truncated: boolean;
  diff_omitted?: "size_cap" | "count_cap" | null;
}

export interface FileTimeline {
  file: RunFile;
  events: EvidenceEvent[];
  truncated: boolean;
  start_state: FileState;
  end_state: FileState;
  diff: FileDiff | null;
  linked_events: { id: string; sequence: number; type: string; summary: string; ts: string }[];
  approvals: {
    id: string;
    title: string;
    status: string;
    type: string;
    reviewer: string | null;
    decided_at: string | null;
  }[];
}

export interface ActivityEntry {
  path: string;
  runs: number;
  operations: number;
  last_at: string | null;
  run_ids: string[];
}

export interface RunFileRef {
  path: string;
  run_id: string;
  run_title: string;
  run_status: string;
  classification: string;
  net_status: string;
  last_at: string | null;
  policy: FilePolicy | null;
}

export interface ProjectFileActivity {
  project_id: string;
  runs_scanned: number;
  runs_with_evidence: number;
  most_touched: ActivityEntry[];
  failed_run_files: ActivityEntry[];
  changed_together: { paths: string[]; runs: number; run_ids: string[] }[];
  protected_activity: RunFileRef[];
  recent: RunFileRef[];
}

export interface FileRunHistoryItem {
  run_id: string;
  run_title: string;
  run_status: string;
  classification: string;
  net_status: string;
  net_unchanged: boolean;
  operation_total: number;
  first_at: string | null;
  last_at: string | null;
  is_protected: boolean;
  insertions: number | null;
  deletions: number | null;
}

/** Ledger filters, mirrored 1:1 onto the list and export query strings. */
export interface LedgerFilters {
  q: string;
  classification: string;
  operation: string;
  net_status: string;
  source: string;
  step: string;
  agent: string;
  protected: boolean;
  outside: boolean;
  generated: boolean;
  net_unchanged: boolean;
  sort: "first_activity" | "last_activity" | "operations" | "path" | "diff_size";
  order: "asc" | "desc";
}

export const EMPTY_FILTERS: LedgerFilters = {
  q: "",
  classification: "",
  operation: "",
  net_status: "",
  source: "",
  step: "",
  agent: "",
  protected: false,
  outside: false,
  generated: false,
  net_unchanged: false,
  sort: "path",
  order: "asc",
};

/** Query parameters for the ledger; toggles are sent only when on (off means "any"). */
export function ledgerQuery(f: LedgerFilters): Record<string, string> {
  const out: Record<string, string> = { sort: f.sort, order: f.order };
  for (const key of [
    "q",
    "classification",
    "operation",
    "net_status",
    "source",
    "step",
    "agent",
  ] as const) {
    const value = f[key].trim();
    if (value) out[key] = value;
  }
  for (const key of ["protected", "outside", "generated", "net_unchanged"] as const) {
    if (f[key]) out[key] = "true";
  }
  return out;
}

const enc = encodeURIComponent;
export const filesKey = (runId: string) => ["run", runId, "files"] as const;
const POLL_MS = 2000;

export function useRunFileSummary(runId: string, live: boolean) {
  return useQuery({
    queryKey: [...filesKey(runId), "summary"],
    queryFn: ({ signal }) =>
      api<FileEvidenceSummary>(`/runs/${enc(runId)}/files/summary`, { signal }),
    refetchInterval: live ? POLL_MS : false,
  });
}

export function useRunFiles(
  runId: string,
  filters: LedgerFilters,
  paging: { limit: number; offset: number },
  live: boolean,
) {
  return useQuery({
    queryKey: [...filesKey(runId), "list", filters, paging],
    queryFn: ({ signal }) =>
      api<Page<RunFile>>(`/runs/${enc(runId)}/files`, {
        query: { ...ledgerQuery(filters), ...paging },
        signal,
      }),
    placeholderData: keepPreviousData,
    refetchInterval: live ? POLL_MS : false,
  });
}

export function useFileTimeline(runId: string, path: string | null, live: boolean) {
  return useQuery({
    queryKey: [...filesKey(runId), "timeline", path],
    queryFn: ({ signal }) =>
      api<FileTimeline>(`/runs/${enc(runId)}/files/timeline`, {
        query: { path: path ?? "" },
        signal,
      }),
    enabled: path !== null,
    refetchInterval: live ? POLL_MS : false,
  });
}

export function useProjectFileActivity(projectId: string) {
  return useQuery({
    queryKey: ["projects", "file-activity", projectId],
    queryFn: ({ signal }) =>
      api<ProjectFileActivity>(`/projects/${enc(projectId)}/file-activity`, { signal }),
  });
}

export function useFileHistory(projectId: string, path: string | null, offset: number) {
  return useQuery({
    queryKey: ["projects", "file-activity", projectId, "history", path, offset],
    queryFn: ({ signal }) =>
      api<Page<FileRunHistoryItem>>(`/projects/${enc(projectId)}/file-activity/history`, {
        query: { path: path ?? "", limit: 20, offset },
        signal,
      }),
    enabled: path !== null,
    placeholderData: keepPreviousData,
  });
}
