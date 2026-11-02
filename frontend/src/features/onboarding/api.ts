import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/api/client";
import type { RunCreateResult } from "@/api/types";

export interface OnboardingState {
  show: boolean;
  dismissed: boolean;
  completed: boolean;
  has_user_project: boolean;
  demo_ready: boolean;
  updated_at: string | null;
}

export const onboardingKey = ["onboarding"] as const;

export function useOnboarding() {
  return useQuery({
    queryKey: onboardingKey,
    queryFn: ({ signal }) => api<OnboardingState>("/onboarding", { signal }),
    staleTime: 30_000,
  });
}

export function useOnboardingAction() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (action: "dismiss" | "complete" | "reset") =>
      api<OnboardingState>("/onboarding", { method: "POST", body: { action } }),
    onSuccess: (state) => client.setQueryData(onboardingKey, state),
  });
}

export function useSeedDemo() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api<unknown>("/demo/seed", { method: "POST" }),
    onSuccess: () => void client.invalidateQueries(),
  });
}

export function useCreateRun() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: {
      agent_profile_id: string;
      prompt_template_id: string;
      prompt_version_id?: string | null;
      project_id?: string | null;
      inputs: Record<string, unknown>;
      source: "ui";
      tags?: string[];
    }) => api<RunCreateResult>("/runs", { method: "POST", body }),
    onSuccess: () => void client.invalidateQueries({ queryKey: ["runs"] }),
  });
}
