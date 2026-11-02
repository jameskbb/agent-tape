/**
 * TanStack Query hooks for the core resources. Feature folders may add their own hooks
 * next to their pages; shared query keys live in {@link queryKeys}.
 */
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
  type QueryKey,
} from "@tanstack/react-query";

import { api } from "./client";
import {
  TERMINAL_STATUSES,
  type AdapterInfo,
  type Approval,
  type DemoScenario,
  type EventPage,
  type Page,
  type Run,
  type RunCreateResult,
  type RunDetail,
  type SystemInfo,
} from "./types";

export const queryKeys = {
  runs: (params: RunListParams) => ["runs", params] as const,
  runsRoot: ["runs"] as const,
  run: (id: string) => ["run", id] as const,
  approvalsRoot: ["approvals"] as const,
  system: ["system"] as const,
  adapters: ["adapters"] as const,
  demoScenarios: ["demo", "scenarios"] as const,
};

export interface RunListParams {
  status?: string[];
  source?: string[];
  q?: string;
  sort?: string;
  order?: "asc" | "desc";
  limit?: number;
  offset?: number;
  is_demo?: boolean;
  project_id?: string;
}

export function useRuns(params: RunListParams, options: { refetchInterval?: number | false } = {}) {
  return useQuery({
    queryKey: queryKeys.runs(params),
    queryFn: ({ signal }) => api<Page<Run>>("/runs", { query: { ...params }, signal }),
    placeholderData: keepPreviousData,
    refetchInterval: options.refetchInterval ?? 5000,
  });
}

export function useRunDetail(id: string, options: { live?: boolean } = {}) {
  return useQuery({
    queryKey: queryKeys.run(id),
    queryFn: ({ signal }) => api<RunDetail>(`/runs/${encodeURIComponent(id)}`, { signal }),
    // Poll while the run is active; stop once it reaches a terminal status.
    refetchInterval: (q) =>
      options.live && !(q.state.data && TERMINAL_STATUSES.has(q.state.data.run.status))
        ? 2000
        : false,
  });
}

export function fetchEventPage(runId: string, afterSequence: number, limit = 500) {
  return api<EventPage>(`/runs/${encodeURIComponent(runId)}/events`, {
    query: { after_sequence: afterSequence, limit },
  });
}

export function useSystemInfo() {
  return useQuery({ queryKey: queryKeys.system, queryFn: () => api<SystemInfo>("/system/info") });
}

export function useAdapters() {
  return useQuery({ queryKey: queryKeys.adapters, queryFn: () => api<AdapterInfo[]>("/adapters") });
}

export function useDemoScenarios() {
  return useQuery({
    queryKey: queryKeys.demoScenarios,
    queryFn: () => api<DemoScenario[]>("/demo/scenarios"),
    staleTime: Infinity,
  });
}

/**
 * Returns a callback that invalidates every listed query key; pass it as a mutation's
 * ``onSuccess``. Feature api modules use it instead of each keeping its own helper.
 */
export function useInvalidate(...keys: QueryKey[]) {
  const client = useQueryClient();
  return () => {
    for (const key of keys) void client.invalidateQueries({ queryKey: key });
  };
}

/** Keys touched by anything that starts, stops, or decides on a run. */
function runKeys(runId?: string): QueryKey[] {
  return [queryKeys.runsRoot, queryKeys.approvalsRoot, ...(runId ? [queryKeys.run(runId)] : [])];
}

export function useStartDemoRun() {
  const invalidate = useInvalidate(...runKeys());
  return useMutation({
    mutationFn: (scenario: string) =>
      api<RunCreateResult>("/demo/runs", { method: "POST", body: { scenario } }),
    onSuccess: invalidate,
  });
}

export function useCancelRun(runId: string) {
  const invalidate = useInvalidate(...runKeys(runId));
  return useMutation({
    mutationFn: (reason?: string) =>
      api<Run>(`/runs/${encodeURIComponent(runId)}/cancel`, {
        method: "POST",
        body: { reason: reason ?? null },
      }),
    onSuccess: invalidate,
  });
}

export function useRetryRun(runId: string) {
  const invalidate = useInvalidate(...runKeys(runId));
  return useMutation({
    mutationFn: () =>
      api<Run>(`/runs/${encodeURIComponent(runId)}/retry`, { method: "POST", body: {} }),
    onSuccess: invalidate,
  });
}

export function useDecideApproval(runId?: string) {
  const invalidate = useInvalidate(...runKeys(runId));
  return useMutation({
    mutationFn: ({
      id,
      decision,
      note,
    }: {
      id: string;
      decision: "approve" | "reject";
      note?: string;
    }) =>
      api<Approval>(`/approvals/${encodeURIComponent(id)}/${decision}`, {
        method: "POST",
        body: { note: note ?? null },
      }),
    onSuccess: invalidate,
  });
}
