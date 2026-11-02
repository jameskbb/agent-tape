/** Projects, path policy, Git state, and activity. */
import { keepPreviousData, useMutation, useQuery } from "@tanstack/react-query";

import { api } from "@/api/client";
import { queryKeys, useInvalidate } from "@/api/hooks";
import type { Page, Run } from "@/api/types";

export interface GitState {
  is_repo: boolean;
  head: string | null;
  branch: string | null;
  dirty: boolean;
  dirty_files: string[];
  dirty_count: number;
  worktree_supported: boolean;
  error: string | null;
}

export interface ProjectSettings {
  timeout_seconds?: number | null;
  idle_timeout_seconds?: number | null;
  max_concurrent_runs?: number | null;
  validation_commands?: string[][];
  protected_paths?: string[];
  env_allowlist?: string[];
  artifact_rules?: Record<string, unknown>;
  git_isolation?: boolean;
  [key: string]: unknown;
}

export interface Project {
  id: string;
  workspace_id: string;
  name: string;
  description: string | null;
  repo_path: string;
  default_branch: string | null;
  tags: string[];
  archived: boolean;
  settings: ProjectSettings;
  git_state: GitState | null;
  git_checked_at: string | null;
  is_demo: boolean;
  created_at: string;
  updated_at: string;
}

export interface PathCheck {
  input: string;
  canonical: string | null;
  allowed: boolean;
  exists: boolean;
  is_dir: boolean;
  is_git_repo: boolean;
  matched_root: string | null;
  allowed_roots: string[];
  message: string;
  git: GitState | null;
}

export interface ActivitySection {
  available: boolean;
  total: number | null;
  items: Record<string, unknown>[];
  note: string | null;
}

export interface ProjectActivity {
  project: Project;
  runs: Run[];
  runs_total: number;
  status_counts: Record<string, number>;
  recent_failures: Run[];
  profiles: { id: string; name: string; adapter_type: string; latest_version: number }[];
  prompts: { id: string; name: string; latest_version: number }[];
  schedules: ActivitySection;
  workflows: ActivitySection;
}

export interface AllowedRoots {
  roots: string[];
  effective: string[];
}

const enc = encodeURIComponent;

export const projectKeys = {
  root: ["projects"] as const,
  list: (p: Record<string, unknown>) => ["projects", "list", p] as const,
  one: (id: string) => ["projects", "one", id] as const,
  activity: (id: string) => ["projects", "activity", id] as const,
  roots: ["allowed-roots"] as const,
  checkPath: ["projects", "check-path"] as const,
};

export function useProjects(params: {
  q?: string;
  include_archived?: boolean;
  limit?: number;
  offset?: number;
}) {
  return useQuery({
    queryKey: projectKeys.list({ ...params }),
    queryFn: ({ signal }) => api<Page<Project>>("/projects", { query: { ...params }, signal }),
    placeholderData: keepPreviousData,
  });
}

export function useProject(id: string) {
  return useQuery({
    queryKey: projectKeys.one(id),
    queryFn: ({ signal }) => api<Project>(`/projects/${enc(id)}`, { signal }),
  });
}

export function useProjectActivity(id: string) {
  return useQuery({
    queryKey: projectKeys.activity(id),
    queryFn: ({ signal }) => api<ProjectActivity>(`/projects/${enc(id)}/activity`, { signal }),
    refetchInterval: 10_000,
  });
}

export function checkPath(path: string) {
  return api<PathCheck>("/projects/check-path", { method: "POST", body: { path } });
}

export function usePathCheck(path: string) {
  return useQuery({
    queryKey: [...projectKeys.checkPath, path] as const,
    queryFn: () => checkPath(path),
    enabled: path.trim().length > 0,
    placeholderData: keepPreviousData,
  });
}

export interface ProjectCreateBody {
  name: string;
  repo_path: string;
  description?: string | null;
  default_branch?: string | null;
  tags?: string[];
  settings?: ProjectSettings;
}

export function useCreateProject() {
  const invalidate = useInvalidate(projectKeys.root);
  return useMutation({
    mutationFn: (body: ProjectCreateBody) => api<Project>("/projects", { method: "POST", body }),
    onSuccess: invalidate,
  });
}

export function useUpdateProject(id: string) {
  const invalidate = useInvalidate(projectKeys.root);
  return useMutation({
    mutationFn: (body: Partial<Omit<ProjectCreateBody, "repo_path">> & { archived?: boolean }) =>
      api<Project>(`/projects/${enc(id)}`, { method: "PATCH", body }),
    onSuccess: invalidate,
  });
}

export function useRefreshGit(id: string) {
  const invalidate = useInvalidate(projectKeys.root);
  return useMutation({
    mutationFn: () => api<Project>(`/projects/${enc(id)}/refresh-git`, { method: "POST" }),
    onSuccess: invalidate,
  });
}

export function useAllowedRoots() {
  return useQuery({
    queryKey: projectKeys.roots,
    queryFn: ({ signal }) => api<AllowedRoots>("/settings/allowed-roots", { signal }),
  });
}

export function useSetAllowedRoots() {
  const invalidate = useInvalidate(projectKeys.roots, projectKeys.checkPath, queryKeys.system);
  return useMutation({
    mutationFn: (roots: string[]) =>
      api<AllowedRoots>("/settings/allowed-roots", { method: "PUT", body: { roots } }),
    onSuccess: invalidate,
  });
}
