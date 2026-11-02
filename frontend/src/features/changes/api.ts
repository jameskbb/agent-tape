/** Types and hooks for a run's Git view (`/runs/{id}/git`), commits, and worktree cleanup. */
import { useMutation, useQuery } from "@tanstack/react-query";

import { api } from "@/api/client";
import { queryKeys, useInvalidate } from "@/api/hooks";
import type { Approval, EvaluationResult } from "@/api/types";

export interface DiffLine {
  kind: "add" | "del" | "ctx" | "note";
  text: string;
  old: number | null;
  new: number | null;
}

export interface DiffHunk {
  header: string;
  section: string;
  lines: DiffLine[];
}

export interface ChangedFile {
  id: string;
  path: string;
  old_path: string | null;
  change_type: "added" | "modified" | "deleted" | "renamed";
  insertions: number | null;
  deletions: number | null;
  is_binary: boolean;
  size_bytes: number | null;
  is_protected: boolean;
  protected_patterns: string[];
  pre_existing_dirty: boolean;
  attempt_id: string | null;
  hunks: DiffHunk[];
  diff_available: boolean;
  diff_truncated: boolean;
  /** Why an untracked file has no hunk, when AgentTape left it out on purpose. */
  diff_omitted?: "size_cap" | "count_cap" | null;
}

export interface GitWorkspace {
  run_id: string;
  isolated: boolean;
  owned: boolean;
  status: string;
  path: string | null;
  repo_path: string | null;
  branch: string | null;
  base_commit: string | null;
  adapter_type: string | null;
  adapter_version: string | null;
  last_commit_sha: string | null;
  commits: {
    sha: string;
    message: string;
    approval_id: string;
    reviewer?: string | null;
    files: number;
    created_at: string;
  }[];
  created_at: string;
  removed_at: string | null;
  uncommitted_count: number | null;
  exists: boolean;
}

export interface StartingRevision {
  is_repo: boolean;
  head: string | null;
  branch: string | null;
  dirty: boolean;
  dirty_count: number;
  dirty_files: string[];
  error: string | null;
}

export interface RunGitView {
  run_id: string;
  project_name: string | null;
  repo_path: string | null;
  git_isolation: boolean;
  protected_patterns: string[];
  validation_commands: string[][];
  starting: StartingRevision | null;
  workspace: GitWorkspace | null;
  summary: {
    files: number;
    insertions: number;
    deletions: number;
    binary_files: number;
    protected_files: number;
    added: number;
    modified: number;
    deleted: number;
    renamed: number;
  };
  files: ChangedFile[];
  attempt_number: number | null;
  diff_artifact_id: string | null;
  diff_truncated: boolean;
  validations: EvaluationResult[];
  commit: {
    available: boolean;
    reason: string | null;
    suggested_message: string;
    pending: Approval | null;
    history: Approval[];
  };
}

export interface CleanupResult {
  removed: boolean;
  message: string;
  path: string;
  branch: string | null;
  branch_deleted: boolean;
  branch_kept_reason: string | null;
  uncommitted: string[];
}

export const gitKey = (runId: string) => ["run", runId, "git"] as const;

export function useRunGit(runId: string, options: { live?: boolean } = {}) {
  return useQuery({
    queryKey: gitKey(runId),
    queryFn: ({ signal }) => api<RunGitView>(`/runs/${encodeURIComponent(runId)}/git`, { signal }),
    refetchInterval: options.live ? 3000 : false,
  });
}

function useInvalidateGit(runId: string) {
  return useInvalidate(gitKey(runId), queryKeys.run(runId), queryKeys.approvalsRoot);
}

export function useRequestCommit(runId: string) {
  const invalidate = useInvalidateGit(runId);
  return useMutation({
    mutationFn: (message: string) =>
      api<Approval>(`/runs/${encodeURIComponent(runId)}/git/commit`, {
        method: "POST",
        body: { message },
      }),
    onSuccess: invalidate,
  });
}

export function useCleanupWorktree(runId: string) {
  const invalidate = useInvalidateGit(runId);
  return useMutation({
    mutationFn: () =>
      api<CleanupResult>(`/runs/${encodeURIComponent(runId)}/git/cleanup`, { method: "POST" }),
    onSuccess: invalidate,
  });
}
