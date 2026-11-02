/** Agent profiles, adapters, secret references, and the executable allowlist. */
import { keepPreviousData, useMutation, useQuery } from "@tanstack/react-query";

import { api } from "@/api/client";
import { useInvalidate } from "@/api/hooks";
import type { FindingsCapture, Page } from "@/api/types";

export type AdapterStatus = "installed" | "unavailable" | "misconfigured" | "ready";

export interface AdapterDetails {
  name: string;
  display_name: string;
  description: string;
  capabilities: string[];
  /** `"per_profile"` when the answer depends on the profile's agent (Herdr); each run then
   * carries its own `Run.findings_capture`. */
  findings: FindingsCapture | "per_profile";
  status: AdapterStatus;
  version: string | null;
  executable: string | null;
  message: string | null;
  details: Record<string, unknown>;
  config_schema: Record<string, unknown>;
}

export type ProfileConfig = Record<string, unknown>;

export interface ProfileVersion {
  id: string;
  profile_id: string;
  version: number;
  adapter_type: string;
  display_name: string;
  description: string | null;
  config: ProfileConfig;
  config_hash: string;
  notes: string | null;
  created_by: string | null;
  created_at: string;
  locked_at: string | null;
}

export interface AgentProfile {
  id: string;
  workspace_id: string;
  project_id: string | null;
  name: string;
  description: string | null;
  adapter_type: string;
  archived: boolean;
  latest_version: number;
  tags: string[];
  is_demo: boolean;
  created_at: string;
  updated_at: string;
  latest: ProfileVersion | null;
}

export interface ValidationIssue {
  field: string;
  message: string;
  severity: "error" | "warning";
}

export interface SecretReference {
  id: string;
  name: string;
  provider: string;
  locator: string;
  description: string | null;
  last_resolved_at: string | null;
  created_at: string;
  updated_at: string;
  available: boolean;
}

export interface ExecutableAllowlist {
  entries: { path: string; source: "env" | "settings"; exists: boolean }[];
  env_var: string;
  stored: string[];
}

export interface ExecutableCheck {
  requested: string;
  path: string | null;
  allowed: boolean;
  allowed_by: string | null;
  error: string | null;
}

const enc = encodeURIComponent;

export const agentKeys = {
  adapters: ["adapters"] as const,
  profiles: (p: Record<string, unknown>) => ["agent-profiles", "list", p] as const,
  profilesRoot: ["agent-profiles"] as const,
  profile: (id: string) => ["agent-profiles", "one", id] as const,
  versions: (id: string) => ["agent-profiles", "versions", id] as const,
  secrets: ["secret-references"] as const,
  executables: ["allowed-executables"] as const,
  validateRoot: ["agent-profiles", "validate"] as const,
};

export function useAdapterDetails() {
  return useQuery({
    queryKey: agentKeys.adapters,
    queryFn: ({ signal }) => api<AdapterDetails[]>("/adapters", { signal }),
    staleTime: 30_000,
  });
}

export interface ProfileListParams {
  q?: string;
  adapter_type?: string;
  include_archived?: boolean;
  limit?: number;
  offset?: number;
}

export function useProfiles(params: ProfileListParams) {
  return useQuery({
    queryKey: agentKeys.profiles({ ...params }),
    queryFn: ({ signal }) =>
      api<Page<AgentProfile>>("/agent-profiles", { query: { ...params }, signal }),
    placeholderData: keepPreviousData,
  });
}

export function useProfile(id: string) {
  return useQuery({
    queryKey: agentKeys.profile(id),
    queryFn: ({ signal }) => api<AgentProfile>(`/agent-profiles/${enc(id)}`, { signal }),
  });
}

export function useProfileVersions(id: string) {
  return useQuery({
    queryKey: agentKeys.versions(id),
    queryFn: ({ signal }) =>
      api<ProfileVersion[]>(`/agent-profiles/${enc(id)}/versions`, { signal }),
  });
}

export function useValidateConfig(adapterType: string, config: ProfileConfig, enabled: boolean) {
  return useQuery({
    queryKey: [...agentKeys.validateRoot, adapterType, config] as const,
    queryFn: ({ signal }) =>
      api<{ valid: boolean; issues: ValidationIssue[] }>("/agent-profiles/validate", {
        method: "POST",
        body: { adapter_type: adapterType, config },
        signal,
      }),
    enabled: enabled && Boolean(adapterType),
    placeholderData: keepPreviousData,
  });
}

export function useCreateProfile() {
  const invalidate = useInvalidate(agentKeys.profilesRoot);
  return useMutation({
    mutationFn: (body: {
      name: string;
      adapter_type: string;
      description?: string | null;
      project_id?: string | null;
      tags?: string[];
      config: ProfileConfig;
      notes?: string | null;
    }) => api<AgentProfile>("/agent-profiles", { method: "POST", body }),
    onSuccess: invalidate,
  });
}

export function useCreateProfileVersion(id: string) {
  const invalidate = useInvalidate(agentKeys.profilesRoot);
  return useMutation({
    mutationFn: (body: {
      config: ProfileConfig;
      description?: string | null;
      notes?: string | null;
    }) => api<ProfileVersion>(`/agent-profiles/${enc(id)}/versions`, { method: "POST", body }),
    onSuccess: invalidate,
  });
}

export function useUpdateProfile(id: string) {
  const invalidate = useInvalidate(agentKeys.profilesRoot);
  return useMutation({
    mutationFn: (body: {
      name?: string;
      description?: string | null;
      tags?: string[];
      project_id?: string | null;
      archived?: boolean;
    }) => api<AgentProfile>(`/agent-profiles/${enc(id)}`, { method: "PATCH", body }),
    onSuccess: invalidate,
  });
}

export function useSecrets() {
  return useQuery({
    queryKey: agentKeys.secrets,
    queryFn: ({ signal }) => api<SecretReference[]>("/secret-references", { signal }),
  });
}

export function useSaveSecret() {
  const invalidate = useInvalidate(agentKeys.secrets);
  return useMutation({
    mutationFn: ({
      id,
      body,
    }: {
      id?: string;
      body: { name?: string; locator?: string | null; description?: string | null };
    }) =>
      id
        ? api<SecretReference>(`/secret-references/${enc(id)}`, { method: "PATCH", body })
        : api<SecretReference>("/secret-references", {
            method: "POST",
            body: { ...body, provider: "env" },
          }),
    onSuccess: invalidate,
  });
}

export function useDeleteSecret() {
  const invalidate = useInvalidate(agentKeys.secrets);
  return useMutation({
    mutationFn: (id: string) =>
      api<undefined>(`/secret-references/${enc(id)}`, { method: "DELETE" }),
    onSuccess: invalidate,
  });
}

export function useExecutables() {
  return useQuery({
    queryKey: agentKeys.executables,
    queryFn: ({ signal }) => api<ExecutableAllowlist>("/settings/allowed-executables", { signal }),
  });
}

export function useSetExecutables() {
  const invalidate = useInvalidate(agentKeys.executables, agentKeys.validateRoot);
  return useMutation({
    mutationFn: (executables: string[]) =>
      api<ExecutableAllowlist>("/settings/allowed-executables", {
        method: "PUT",
        body: { executables },
      }),
    onSuccess: invalidate,
  });
}

export function checkExecutable(executable: string, adapterType?: string) {
  return api<ExecutableCheck>("/settings/allowed-executables/check", {
    method: "POST",
    body: { executable, adapter_type: adapterType ?? null },
  });
}
