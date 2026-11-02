/**
 * Types and hooks for schedules, webhook triggers, and limits. Mirrors
 * `backend/src/agenttape/application/schemas/schedules.py`.
 */
import { keepPreviousData, useMutation, useQuery } from "@tanstack/react-query";

import { api } from "@/api/client";
import { queryKeys, useInvalidate } from "@/api/hooks";
import type { Page, Run } from "@/api/types";

export type ScheduleKind = "one_time" | "interval" | "cron";
export type MisfirePolicy = "skip" | "run_once";
export type ConcurrencyPolicy = "queue" | "skip" | "cancel_and_replace";
export type TargetType = "prompt" | "workflow";

export interface Schedule {
  id: string;
  project_id: string | null;
  name: string;
  description: string | null;
  target_type: TargetType;
  prompt_template_id: string | null;
  prompt_version_id: string | null;
  agent_profile_id: string | null;
  workflow_id: string | null;
  workflow_version_id: string | null;
  inputs: Record<string, unknown>;
  kind: ScheduleKind;
  cron_expression: string | null;
  interval_seconds: number | null;
  run_at: string | null;
  timezone: string;
  enabled: boolean;
  start_at: string | null;
  end_at: string | null;
  misfire_policy: MisfirePolicy;
  concurrency_policy: ConcurrencyPolicy;
  max_queued: number;
  priority: number;
  next_run_at: string | null;
  last_run_at: string | null;
  last_run_id: string | null;
  is_demo: boolean;
  created_at: string;
  updated_at: string;
  target_name: string | null;
  agent_profile_name: string | null;
  project_name: string | null;
  last_run_status: Run["status"] | null;
  active_runs: number;
  queued_occurrences: number;
  last_occurrence_status: string | null;
  last_occurrence_reason: string | null;
  next_run_local: string | null;
  run_at_local: string | null;
  start_at_local: string | null;
  end_at_local: string | null;
}

export interface ScheduleTiming {
  kind: ScheduleKind;
  timezone: string;
  cron_expression?: string | null;
  interval_seconds?: number | null;
  run_at?: string | null;
  start_at?: string | null;
  end_at?: string | null;
}

export interface ScheduleInput extends ScheduleTiming {
  name: string;
  description?: string | null;
  target_type: TargetType;
  project_id?: string | null;
  prompt_template_id?: string | null;
  agent_profile_id?: string | null;
  workflow_id?: string | null;
  inputs: Record<string, unknown>;
  enabled: boolean;
  misfire_policy: MisfirePolicy;
  concurrency_policy: ConcurrencyPolicy;
  max_queued: number;
}

export interface Occurrence {
  id: string;
  schedule_id: string;
  occurrence_at: string;
  status: "pending" | "queued" | "created" | "skipped" | "missed" | "replaced" | "failed";
  run_id: string | null;
  reason: string | null;
  created_at: string;
  run_status: Run["status"] | null;
  occurrence_local: string | null;
}

export interface PreviewItem {
  at: string;
  local: string;
  utc_offset: string;
  note: string | null;
}

export interface SchedulePreview {
  valid: boolean;
  error: string | null;
  timezone: string;
  occurrences: PreviewItem[];
  finished: boolean;
}

export interface RunNowResult {
  occurrence: Occurrence;
  run_id: string | null;
  action: "start" | "wait" | "skip" | "replace";
  message: string;
}

export interface SchedulerStatus {
  state: "running" | "standby" | "stopped" | "unknown";
  owner_id: string | null;
  lease_expires_at: string | null;
  last_tick_at: string | null;
  message: string;
}

export interface VariableDef {
  name: string;
  type: "string" | "multiline" | "number" | "boolean" | "enum" | "path" | "secret";
  label?: string | null;
  required?: boolean;
  default?: unknown;
  help?: string | null;
  options?: string[] | null;
}

export interface TargetOption {
  id: string;
  name: string;
  description: string | null;
  latest_version: number | null;
  latest_version_id: string | null;
  variables: VariableDef[];
  adapter_type: string | null;
  project_id: string | null;
  is_demo: boolean;
}

export interface ScheduleTargets {
  prompts: TargetOption[];
  agent_profiles: TargetOption[];
  projects: TargetOption[];
  workflows: TargetOption[];
  workflow_runs_available: boolean;
}

// ----------------------------------------------------------------------------- triggers

export interface Trigger {
  id: string;
  project_id: string | null;
  name: string;
  description: string | null;
  kind: string;
  target_type: TargetType;
  prompt_template_id: string | null;
  prompt_version_id: string | null;
  agent_profile_id: string | null;
  workflow_id: string | null;
  token_prefix: string | null;
  input_mapping: Record<string, string>;
  idempotency_key_path: string | null;
  enabled: boolean;
  rate_limit_per_minute: number;
  max_payload_bytes: number;
  config: {
    static_inputs?: Record<string, unknown>;
    idempotency?: "derive" | "require";
    /** Accept `?token=` (off by default; URLs end up in logs). */
    allow_query_token?: boolean;
  };
  last_delivery_at: string | null;
  is_demo: boolean;
  created_at: string;
  webhook_path: string;
  target_name: string | null;
  agent_profile_name: string | null;
  delivery_count: number;
}

export interface TriggerInput {
  name: string;
  description?: string | null;
  target_type: TargetType;
  project_id?: string | null;
  prompt_template_id?: string | null;
  agent_profile_id?: string | null;
  workflow_id?: string | null;
  input_mapping: Record<string, string>;
  idempotency_key_path?: string | null;
  idempotency: "derive" | "require";
  rate_limit_per_minute: number;
  max_payload_bytes: number;
}

export interface TriggerWithToken {
  trigger: Trigger;
  token: string;
}

export interface Delivery {
  id: string;
  trigger_id: string;
  received_at: string;
  idempotency_key: string;
  status: "accepted" | "duplicate" | "rejected" | "error";
  http_status: number;
  run_id: string | null;
  payload_bytes: number;
  metadata: {
    headers?: Record<string, string>;
    idempotency_source?: string;
    payload_preview?: string;
    code?: string;
    auth?: string;
  };
  error: string | null;
}

export interface MappingTestResult {
  inputs: Record<string, unknown>;
  missing: string[];
  errors: string[];
}

// ----------------------------------------------------------------------------- limits

export interface LimitsSettings {
  max_concurrent_runs: number | null;
  per_adapter: Record<string, number>;
  default_budget: Record<string, number>;
}

export interface BudgetKey {
  key: string;
  label: string;
  unit: string;
  description: string;
}

export interface LimitsOut {
  settings: LimitsSettings;
  budget_keys: BudgetKey[];
  active_runs: number;
  active_by_adapter: Record<string, number>;
  active_by_project: Record<string, number>;
  worker_defaults: Record<string, number>;
}

export interface BudgetCheck {
  key: string;
  label: string;
  unit: string;
  limit: number;
  used: number | null;
  ratio: number | null;
  status: "ok" | "warning" | "exceeded" | "not_applicable" | "unknown";
  note: string | null;
}

export interface RunLimits {
  run_id: string;
  budget: Record<string, number>;
  budget_sources: Record<string, string>;
  checks: BudgetCheck[];
  timeout_seconds: number | null;
  idle_timeout_seconds: number | null;
  max_attempts: number;
  attempt_count: number;
  retry: {
    retry_on: string[];
    backoff_base_seconds: number;
    backoff_max_seconds: number;
    jitter_ratio: number;
    configured: Record<string, unknown>;
    delays_seconds: number[];
  };
  concurrency: { scope: string; label: string; limit: number; running: number }[];
}

// ----------------------------------------------------------------------------- hooks

export const scheduleKeys = {
  root: ["schedules"] as const,
  list: (params: Record<string, unknown>) => ["schedules", "list", params] as const,
  one: (id: string) => ["schedules", "one", id] as const,
  history: (id: string, offset: number) => ["schedules", "history", id, offset] as const,
  runs: (id: string) => ["schedules", "runs", id] as const,
  status: ["scheduler", "status"] as const,
  targets: ["schedules", "targets"] as const,
  timezones: ["schedules", "timezones"] as const,
  preview: (timing: unknown) => ["schedules", "preview", timing] as const,
  triggers: ["triggers"] as const,
  trigger: (id: string) => ["triggers", id] as const,
  deliveries: (id: string, offset: number) => ["triggers", id, "deliveries", offset] as const,
  limits: ["limits"] as const,
  runLimits: (id: string) => ["limits", "run", id] as const,
};

export function useSchedules(params: { q?: string; offset?: number; limit?: number }) {
  return useQuery({
    queryKey: scheduleKeys.list(params),
    queryFn: ({ signal }) =>
      api<Page<Schedule>>("/schedules", { query: { ...params, sort: "name" }, signal }),
    placeholderData: keepPreviousData,
    refetchInterval: 10_000,
  });
}

export function useSchedule(id: string) {
  return useQuery({
    queryKey: scheduleKeys.one(id),
    queryFn: ({ signal }) => api<Schedule>(`/schedules/${encodeURIComponent(id)}`, { signal }),
    enabled: id !== "",
    refetchInterval: 5_000,
  });
}

export function useScheduleHistory(id: string, offset: number) {
  return useQuery({
    queryKey: scheduleKeys.history(id, offset),
    queryFn: ({ signal }) =>
      api<Page<Occurrence>>(`/schedules/${encodeURIComponent(id)}/history`, {
        query: { limit: 20, offset },
        signal,
      }),
    placeholderData: keepPreviousData,
    refetchInterval: 5_000,
  });
}

export function useSchedulerStatus() {
  return useQuery({
    queryKey: scheduleKeys.status,
    queryFn: ({ signal }) => api<SchedulerStatus>("/scheduler/status", { signal }),
    refetchInterval: 10_000,
  });
}

export function useScheduleTargets() {
  return useQuery({
    queryKey: scheduleKeys.targets,
    queryFn: ({ signal }) => api<ScheduleTargets>("/schedules/targets", { signal }),
  });
}

export function useTimezones() {
  return useQuery({
    queryKey: scheduleKeys.timezones,
    queryFn: ({ signal }) => api<string[]>("/schedules/timezones", { signal }),
    staleTime: Infinity,
  });
}

export function useSchedulePreview(timing: (ScheduleTiming & { count: number }) | null) {
  return useQuery({
    queryKey: scheduleKeys.preview(timing),
    queryFn: ({ signal }) =>
      api<SchedulePreview>("/schedules/preview", { method: "POST", body: timing, signal }),
    enabled: timing !== null,
    placeholderData: keepPreviousData,
  });
}

function useInvalidateSchedules() {
  return useInvalidate(scheduleKeys.root, queryKeys.runsRoot);
}

export function useSaveSchedule(id?: string) {
  const invalidate = useInvalidateSchedules();
  return useMutation({
    mutationFn: (body: ScheduleInput) =>
      id
        ? api<Schedule>(`/schedules/${encodeURIComponent(id)}`, { method: "PATCH", body })
        : api<Schedule>("/schedules", { method: "POST", body }),
    onSuccess: invalidate,
  });
}

export function useScheduleAction(id: string) {
  const invalidate = useInvalidateSchedules();
  return useMutation({
    mutationFn: (action: "pause" | "resume") =>
      api<Schedule>(`/schedules/${encodeURIComponent(id)}/${action}`, { method: "POST" }),
    onSuccess: invalidate,
  });
}

export function useRunNow(id: string) {
  const invalidate = useInvalidateSchedules();
  return useMutation({
    mutationFn: (force: boolean) =>
      api<RunNowResult>(`/schedules/${encodeURIComponent(id)}/run-now`, {
        method: "POST",
        body: { force },
      }),
    onSuccess: invalidate,
  });
}

export function useDeleteSchedule(id: string) {
  const invalidate = useInvalidateSchedules();
  return useMutation({
    mutationFn: () => api<unknown>(`/schedules/${encodeURIComponent(id)}`, { method: "DELETE" }),
    onSuccess: invalidate,
  });
}

export function useScheduleRuns(id: string) {
  return useQuery({
    queryKey: scheduleKeys.runs(id),
    queryFn: ({ signal }) =>
      api<Page<Run>>("/runs", { query: { schedule_id: id, limit: 10 }, signal }),
    refetchInterval: 5_000,
  });
}

export function useTriggers(page: { offset: number; limit: number } = { offset: 0, limit: 50 }) {
  return useQuery({
    queryKey: [...scheduleKeys.triggers, page.offset, page.limit],
    queryFn: ({ signal }) => api<Page<Trigger>>("/triggers", { query: { ...page }, signal }),
    placeholderData: keepPreviousData,
    refetchInterval: 10_000,
  });
}

export function useTrigger(id: string) {
  return useQuery({
    queryKey: scheduleKeys.trigger(id),
    queryFn: ({ signal }) => api<Trigger>(`/triggers/${encodeURIComponent(id)}`, { signal }),
  });
}

export function useDeliveries(id: string, offset: number) {
  return useQuery({
    queryKey: scheduleKeys.deliveries(id, offset),
    queryFn: ({ signal }) =>
      api<Page<Delivery>>(`/triggers/${encodeURIComponent(id)}/deliveries`, {
        query: { limit: 20, offset },
        signal,
      }),
    placeholderData: keepPreviousData,
    refetchInterval: 5_000,
  });
}

export function useLimits() {
  return useQuery({
    queryKey: scheduleKeys.limits,
    queryFn: ({ signal }) => api<LimitsOut>("/settings/limits", { signal }),
    refetchInterval: 10_000,
  });
}

export function useRunLimits(runId: string, live: boolean) {
  return useQuery({
    queryKey: scheduleKeys.runLimits(runId),
    queryFn: ({ signal }) =>
      api<RunLimits>(`/runs/${encodeURIComponent(runId)}/limits`, { signal }),
    refetchInterval: live ? 3_000 : false,
  });
}
