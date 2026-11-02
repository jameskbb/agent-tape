import { keepPreviousData, useQuery } from "@tanstack/react-query";

import { api } from "@/api/client";
import type { Approval, Run } from "@/api/types";
import type { FindingsTally } from "@/features/findings/api";

export interface DayActivity {
  date: string;
  succeeded: number;
  failed: number;
  other: number;
}

export interface Dashboard {
  generated_at: string;
  window_days: number;
  live: Record<string, number>;
  live_runs: Run[];
  pending_approvals: Approval[];
  pending_approval_count: number;
  upcoming_schedules: {
    id: string;
    name: string;
    kind: string;
    timezone: string;
    next_run_at: string;
  }[];
  enabled_schedule_count: number;
  recent_failures: Run[];
  runs_in_window: number;
  success_rate: { succeeded: number; finished: number; rate: number | null };
  duration: { median_ms: number | null; p90_ms: number | null; sample: number };
  activity: DayActivity[];
  failure_categories: { category: string; count: number }[];
  usage: { runs_reporting: number; total_tokens: number | null; simulated_runs: number };
  cost: { runs_reporting: number; total_usd: number | null };
  findings: FindingsTally;
}

export function useDashboard(days: number) {
  return useQuery({
    queryKey: ["dashboard", days],
    queryFn: ({ signal }) => api<Dashboard>("/dashboard", { query: { days }, signal }),
    placeholderData: keepPreviousData,
    refetchInterval: 5000,
  });
}
