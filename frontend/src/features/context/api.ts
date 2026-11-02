/** Types and hooks for context evidence, context packs, provenance, and the learning loop. */
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/api/client";

export type Stage =
  | "available"
  | "selected"
  | "retrieved"
  | "injected"
  | "referenced"
  | "cited"
  | "unavailable"
  | "unknown"
  | "excluded"
  | "conflict"
  | "stale"
  | "gap";

export interface EvidenceRow {
  id: string;
  ts: string | null;
  sequence: number;
  position: number;
  attempt_id: string | null;
  node_key: string | null;
  event_id: string | null;
  stage: Stage;
  source_id: string | null;
  source_key: string | null;
  source_type: string | null;
  source_name: string | null;
  provider: string | null;
  authority: string | null;
  live: boolean | null;
  freshness: "live" | "snapshot" | "stale" | "unknown";
  version: string | null;
  synced_at: string | null;
  quality: string;
  query: string | null;
  tool: {
    server?: string | null;
    name?: string | null;
    args?: unknown;
    status?: string | null;
    started_at?: string;
    ended_at?: string;
  } | null;
  result: {
    hash?: string;
    bytes?: number | null;
    rows?: number | null;
    schema?: string[] | null;
    truncated?: boolean | null;
    media_type?: string | null;
  } | null;
  reason: string | null;
  details: Record<string, unknown>;
  cited_in_run: boolean;
}

export interface PackItem {
  position: number;
  kind: string;
  source_key: string;
  source_type: string;
  name: string;
  stage: Stage;
  reason: string;
  locator: string | null;
  authority: string;
  live: boolean | null;
  version: string | null;
  content_hash: string | null;
  chars: number | null;
  excerpt: string | null;
  details: Record<string, unknown>;
}

export interface ExcludedItem {
  kind: string | null;
  name: string | null;
  reason: string | null;
  source_key?: string | null;
}

export interface ContextPack {
  id: string;
  run_id: string;
  attempt_id: string;
  node_key: string | null;
  version: number;
  policy: string;
  policy_version: number;
  created_at: string | null;
  content_hash: string;
  token_estimate: number | null;
  token_method: string | null;
  token_status: "reported" | "unavailable";
  items: PackItem[];
  excluded: ExcludedItem[];
  redaction: Record<string, unknown>;
  injected_text: string | null;
}

export type Classification =
  "run-specific" | "source-correction" | "business-knowledge" | "agent-behavior";
export type Destination =
  | "prompt_revision"
  | "project_instruction"
  | "knowledge_source_update"
  | "context_provider_rule"
  | "no_promotion";

export interface Feedback {
  id: string;
  run_id: string;
  project_id: string | null;
  kind: string;
  text: string;
  source_key: string | null;
  event_id: string | null;
  reported_by: string;
  suggested_classification: Classification | null;
  status: "pending" | "approved" | "rejected";
  classification: Classification | null;
  proposed_destination: Destination | null;
  injects: boolean;
  lesson_text: string | null;
  lesson_version: number | null;
  reviewed_by: string | null;
  reviewed_at: string | null;
  review_note: string | null;
  created_at: string | null;
  injected_runs?: number;
}

export interface ContextSummary {
  run_id: string;
  status: string;
  statement: string;
  trigger: Record<string, unknown>;
  original_request: string | null;
  prompt: {
    name: string | null;
    version: number | null;
    template_hash: string | null;
    rendered_hash: string | null;
    stage: Stage | null;
  } | null;
  agent: {
    profile: string | null;
    profile_version: number | null;
    adapter_type: string | null;
    model: string | null;
  };
  pack: {
    id: string;
    version: number;
    versions: number;
    policy: string;
    content_hash: string;
    created_at: string | null;
    items: number;
    excluded: number;
    token_estimate: number | null;
    token_status: "reported" | "unavailable";
  } | null;
  instructions: PackItem[];
  skills: PackItem[];
  lessons: PackItem[];
  excluded: ExcludedItem[];
  stages: { stage: Stage; records: number; sources: number }[];
  live_sources: number;
  snapshot_sources: number;
  freshness_warnings: EvidenceRow[];
  conflicts: EvidenceRow[];
  gaps: EvidenceRow[];
  findings: number;
  feedback: Feedback[];
  usage: { status: "reported" | "simulated" | "unavailable"; values: Record<string, number> };
  window: {
    complete: boolean;
    offline: { offline_from: string | null; offline_to: string | null }[];
  };
}

export interface EvidenceFilters {
  stage?: string;
  source_type?: string;
  provider?: string;
  node_key?: string;
  authority?: string;
  freshness?: string;
  quality?: string;
  q?: string;
}

export interface EvidencePage {
  items: EvidenceRow[];
  total: number;
  limit: number;
  offset: number;
  next_offset: number | null;
  facets: Record<keyof Omit<EvidenceFilters, "q">, string[]>;
}

export interface SourceDetail {
  source: {
    id: string;
    key: string;
    type: string;
    name: string;
    provider: string | null;
    locator: string | null;
    authority: string;
    live: boolean | null;
    version: string | null;
    synced_at: string | null;
    sensitivity: string;
    retention: string;
    first_seen_at: string | null;
    last_seen_at: string | null;
  };
  freshness: string;
  access: EvidenceRow[];
  links: {
    subject_type: string;
    subject_key: string;
    subject_text: string | null;
    relation: string;
    origin: string;
  }[];
  other_runs: number;
  excerpts: { pack_version: number; text: string; redacted: boolean }[];
  file_path: string | null;
}

export interface ContextComparison {
  a_run_id: string;
  b_run_id: string;
  same_pack: boolean;
  sources: {
    key: string;
    name: string;
    a_stages: string[];
    b_stages: string[];
    change: "added" | "removed" | "changed" | "same";
  }[];
  counts: Partial<Record<"added" | "removed" | "changed" | "same", number>>;
  prompt: {
    a: { name: string | null; version: string | null; hash: string | null };
    b: { name: string | null; version: string | null; hash: string | null };
    same: boolean;
  };
  instructions: { name: string; a_hash: string | null; b_hash: string | null; changed: boolean }[];
  lessons: { a_only: string[]; b_only: string[] };
  versions: {
    key: string;
    name: string;
    a: { version: string | null; freshness: string };
    b: { version: string | null; freshness: string };
  }[];
  queries: { a_only: string[]; b_only: string[] };
  mcp: { tool: string; a: string | null; b: string | null }[];
  gaps: { resolved: string[]; introduced: string[] };
  citations: { a_only: string[]; b_only: string[] };
  conclusions: { a_only: string[]; b_only: string[] };
}

export interface SourceMetric {
  source: { id: string; key?: string; name?: string; type?: string; live?: boolean | null };
  count: number;
  run_count: number;
  runs: string[];
}

export interface ContextActivity {
  project_id: string;
  note: string;
  most_retrieved: SourceMetric[];
  most_cited: SourceMetric[];
  rarely_selected: SourceMetric[];
  stale_or_unavailable: SourceMetric[];
  gaps_by_category: { kind: string; count: number; runs: string[] }[];
  runs_with_missing_context_and_failure_or_correction: string[];
  prompt_versions: {
    prompt: string;
    version: number | null;
    runs: string[];
    passed: number;
    failed: number;
    evaluated: boolean;
  }[];
  lessons: Feedback[];
  pending_feedback: number;
}

const enc = encodeURIComponent;
export const contextKeys = {
  run: (runId: string) => ["context", runId] as const,
};

export function useContextSummary(runId: string, live: boolean) {
  return useQuery({
    queryKey: [...contextKeys.run(runId), "summary"],
    queryFn: ({ signal }) => api<ContextSummary>(`/runs/${enc(runId)}/context`, { signal }),
    refetchInterval: live ? 3000 : false,
  });
}

export function useContextPacks(runId: string, live: boolean) {
  return useQuery({
    queryKey: [...contextKeys.run(runId), "packs"],
    queryFn: ({ signal }) => api<ContextPack[]>(`/runs/${enc(runId)}/context/packs`, { signal }),
    refetchInterval: live ? 5000 : false,
  });
}

export function useEvidence(
  runId: string,
  filters: EvidenceFilters,
  offset: number,
  limit: number,
  live: boolean,
) {
  return useQuery({
    queryKey: [...contextKeys.run(runId), "evidence", filters, offset, limit],
    queryFn: ({ signal }) =>
      api<EvidencePage>(`/runs/${enc(runId)}/context/evidence`, {
        query: { ...filters, offset, limit },
        signal,
      }),
    placeholderData: keepPreviousData,
    refetchInterval: live ? 3000 : false,
  });
}

export function useSourceDetail(runId: string, sourceId: string | null) {
  return useQuery({
    queryKey: [...contextKeys.run(runId), "source", sourceId],
    queryFn: ({ signal }) =>
      api<SourceDetail>(`/runs/${enc(runId)}/context/sources/${enc(sourceId ?? "")}`, { signal }),
    enabled: Boolean(sourceId),
  });
}

export function useContextComparison(a: string | null, b: string | null) {
  return useQuery({
    queryKey: ["context-compare", a, b],
    queryFn: ({ signal }) =>
      api<ContextComparison>(`/runs/${enc(a ?? "")}/context/compare/${enc(b ?? "")}`, { signal }),
    enabled: Boolean(a && b && a !== b),
  });
}

export function useContextActivity(projectId: string | null) {
  return useQuery({
    queryKey: ["context-activity", projectId],
    queryFn: ({ signal }) =>
      api<ContextActivity>(`/projects/${enc(projectId ?? "")}/context/activity`, { signal }),
    enabled: Boolean(projectId),
  });
}

export type FeedbackAction =
  | { action: "classify"; id: string; classification: Classification }
  | { action: "propose"; id: string; destination: Destination; lesson_text?: string }
  | { action: "approve"; id: string; note?: string }
  | { action: "reject"; id: string; note?: string }
  | { action: "create"; runId: string; kind: string; text: string };

export function useFeedbackAction(runId: string | null) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (a: FeedbackAction) => {
      if (a.action === "create")
        return api<Feedback>(`/runs/${enc(a.runId)}/context/feedback`, {
          method: "POST",
          body: { kind: a.kind, text: a.text },
        });
      const { action, id, ...body } = a;
      return api<Feedback>(`/context/feedback/${enc(id)}/${action}`, { method: "POST", body });
    },
    onSuccess: () => {
      if (runId) void client.invalidateQueries({ queryKey: contextKeys.run(runId) });
      void client.invalidateQueries({ queryKey: ["context-activity"] });
    },
  });
}
