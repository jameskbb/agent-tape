/** Types and hooks for the Herdr runtime API (`/runtime/herdr`, `/runs/{id}/runtime`). */
import { useMutation, useQuery } from "@tanstack/react-query";

import { api } from "@/api/client";

export interface RuntimeCapability {
  name: string;
  description: string;
  support: "supported" | "degraded" | "unsupported";
  note: string;
}

export interface RuntimeSession {
  attempt_id: string;
  attempt_number: number | null;
  runtime: string;
  machine: string;
  session_name: string | null;
  herdr_version: string | null;
  protocol: number | null;
  workspace_id: string | null;
  tab_id: string | null;
  pane_id: string | null;
  agent_name: string | null;
  agent_kind: string | null;
  native_session_ref: string;
  lifecycle_state: string | null;
  owned_workspace: boolean;
  owned_agent: boolean;
  last_sync_at: string | null;
  status: string;
  failure_category: string | null;
  fallback_to: string | null;
  attach: { command: string | null; guidance: string };
  capabilities: RuntimeCapability[];
}

export interface RunRuntimeView {
  run_id: string;
  adapter_type: string | null;
  runtime: string;
  sessions: RuntimeSession[];
}

/** A saved Herdr machine (display fields only; the list is read-only). */
export interface HerdrMachine {
  label?: string;
  name?: string;
  ssh_target?: string;
  target?: string;
  host?: string;
  enabled?: boolean;
  remote_session?: string;
}

export interface HerdrStatus {
  installed: boolean;
  cli_version: string | null;
  reachable: boolean;
  version: string | null;
  protocol: number | null;
  protocol_supported: boolean | null;
  message: string;
  session: string;
  machine: string;
  server_capabilities: Record<string, unknown>;
  runtime_capabilities: Record<string, RuntimeCapability[]>;
  machines: HerdrMachine[] | null;
  integrations: { target: string; label: string; available: boolean; state: string }[] | null;
  defaults: Record<string, string>;
}

export interface HerdrTest {
  ok: boolean;
  checks: { name: string; ok: boolean; detail: string; hint: string | null }[];
}

export function useHerdrStatus() {
  return useQuery({
    queryKey: ["runtime", "herdr"],
    queryFn: ({ signal }) => api<HerdrStatus>("/runtime/herdr", { signal }),
  });
}

export function useHerdrTest() {
  return useMutation({
    mutationFn: () => api<HerdrTest>("/runtime/herdr/test", { method: "POST" }),
  });
}

export function useRunRuntime(runId: string, live: boolean) {
  return useQuery({
    queryKey: ["run", runId, "runtime"],
    queryFn: ({ signal }) =>
      api<RunRuntimeView>(`/runs/${encodeURIComponent(runId)}/runtime`, { signal }),
    refetchInterval: live ? 2000 : false,
  });
}
