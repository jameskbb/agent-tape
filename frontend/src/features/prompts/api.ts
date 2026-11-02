/** Prompt library types (mirroring backend schemas/catalog.py + library.py) and hooks. */
import { keepPreviousData, useMutation, useQuery } from "@tanstack/react-query";

import { api, saveBlob } from "@/api/client";
import { queryKeys, useInvalidate } from "@/api/hooks";
import type { Page, Run } from "@/api/types";

export type VariableType =
  "string" | "multiline" | "number" | "boolean" | "enum" | "path" | "secret";

export const VARIABLE_TYPES: { value: VariableType; label: string; hint: string }[] = [
  { value: "string", label: "Text", hint: "One line of text" },
  { value: "multiline", label: "Multiline text", hint: "Paragraphs, logs, notes" },
  { value: "number", label: "Number", hint: "Integer or decimal" },
  { value: "boolean", label: "Yes / no", hint: "Renders as true or false" },
  { value: "enum", label: "Choice", hint: "One of a fixed list of options" },
  { value: "path", label: "Path", hint: "A path; '..' segments are rejected" },
  {
    value: "secret",
    label: "Secret reference",
    hint: "The name of a secret; the value is never stored",
  },
];

export interface VariableValidation {
  pattern?: string | null;
  min_length?: number | null;
  max_length?: number | null;
  minimum?: number | null;
  maximum?: number | null;
}

export interface VariableDefinition {
  name: string;
  type: VariableType;
  label?: string | null;
  required?: boolean;
  default?: unknown;
  help?: string | null;
  options?: string[] | null;
  validation?: VariableValidation | null;
}

export interface PromptVersion {
  id: string;
  template_id: string;
  version: number;
  body: string;
  variables: VariableDefinition[];
  settings: Record<string, unknown>;
  content_hash: string;
  notes: string | null;
  created_by: string | null;
  created_at: string;
  locked_at: string | null;
}

export interface PromptTemplate {
  id: string;
  workspace_id: string;
  project_id: string | null;
  name: string;
  description: string | null;
  tags: string[];
  owner: string | null;
  archived: boolean;
  latest_version: number;
  is_demo: boolean;
  created_at: string;
  updated_at: string;
  latest: PromptVersion | null;
}

export interface RenderPreview {
  text: string;
  secret_refs: string[];
  issues: { variable: string; message: string }[];
  valid: boolean;
}

export interface DiffLine {
  op: "context" | "add" | "remove";
  text: string;
  old_no: number | null;
  new_no: number | null;
}

export interface PromptDiff {
  template_id: string;
  from_version: number;
  to_version: number;
  unified: string;
  lines: DiffLine[];
  variables_added: string[];
  variables_removed: string[];
  variables_changed: string[];
  body_changed: boolean;
}

export interface PromptExportBundle {
  format: "agenttape.prompts";
  format_version: number;
  exported_at: string | null;
  templates: unknown[];
}

export interface PromptListParams {
  q?: string;
  tag?: string[];
  owner?: string;
  archived?: "active" | "archived" | "all";
  sort?: string;
  order?: "asc" | "desc";
  limit?: number;
  offset?: number;
}

export interface PromptCreateBody {
  name: string;
  description?: string | null;
  tags?: string[];
  owner?: string | null;
  project_id?: string | null;
  body: string;
  variables: VariableDefinition[];
  notes?: string | null;
}

const enc = encodeURIComponent;

export const promptKeys = {
  root: ["prompts"] as const,
  list: (p: PromptListParams) => ["prompts", "list", p] as const,
  one: (id: string) => ["prompts", "one", id] as const,
  versions: (id: string) => ["prompts", "versions", id] as const,
  tags: ["prompts", "tags"] as const,
  diff: (id: string, a: number, b: number) => ["prompts", "diff", id, a, b] as const,
};

export function usePrompts(params: PromptListParams) {
  return useQuery({
    queryKey: promptKeys.list(params),
    queryFn: ({ signal }) =>
      api<Page<PromptTemplate>>("/prompts", { query: { ...params }, signal }),
    placeholderData: keepPreviousData,
  });
}

export function usePrompt(id: string) {
  return useQuery({
    queryKey: promptKeys.one(id),
    queryFn: ({ signal }) => api<PromptTemplate>(`/prompts/${enc(id)}`, { signal }),
  });
}

export function usePromptVersions(id: string) {
  return useQuery({
    queryKey: promptKeys.versions(id),
    queryFn: ({ signal }) => api<PromptVersion[]>(`/prompts/${enc(id)}/versions`, { signal }),
  });
}

export function usePromptTags() {
  return useQuery({
    queryKey: promptKeys.tags,
    queryFn: ({ signal }) => api<string[]>("/prompt-tags", { signal }),
  });
}

export function usePromptDiff(id: string, from: number, to: number, enabled: boolean) {
  return useQuery({
    queryKey: promptKeys.diff(id, from, to),
    queryFn: ({ signal }) =>
      api<PromptDiff>(`/prompts/${enc(id)}/diff`, { query: { from, to }, signal }),
    enabled,
  });
}

export function renderPrompt(id: string, inputs: Record<string, unknown>, versionId?: string) {
  return api<RenderPreview>(`/prompts/${enc(id)}/render`, {
    method: "POST",
    body: { inputs, version_id: versionId ?? null },
  });
}

/** Live rendered preview (secret references stay as [secret:NAME] markers). */
export function useRenderPreview(
  id: string | null,
  versionId: string | null,
  inputs: Record<string, unknown>,
) {
  return useQuery({
    queryKey: ["prompts", "render", id, versionId, inputs] as const,
    queryFn: () => renderPrompt(id ?? "", inputs, versionId ?? undefined),
    enabled: Boolean(id),
    placeholderData: keepPreviousData,
    staleTime: 60_000,
  });
}

export function useCreatePrompt() {
  const invalidate = useInvalidate(promptKeys.root);
  return useMutation({
    mutationFn: (body: PromptCreateBody) =>
      api<PromptTemplate>("/prompts", { method: "POST", body }),
    onSuccess: invalidate,
  });
}

export function useUpdatePrompt(id: string) {
  const invalidate = useInvalidate(promptKeys.root);
  return useMutation({
    mutationFn: (body: Partial<Omit<PromptTemplate, "id" | "latest">>) =>
      api<PromptTemplate>(`/prompts/${enc(id)}`, { method: "PATCH", body }),
    onSuccess: invalidate,
  });
}

export function useSaveVersion(id: string) {
  const invalidate = useInvalidate(promptKeys.root);
  return useMutation({
    mutationFn: (body: { body: string; variables: VariableDefinition[]; notes?: string | null }) =>
      api<PromptVersion>(`/prompts/${enc(id)}/versions/save`, { method: "POST", body }),
    onSuccess: invalidate,
  });
}

export function useDuplicatePrompt(id: string) {
  const invalidate = useInvalidate(promptKeys.root);
  return useMutation({
    mutationFn: (body: { name?: string | null; version_id?: string | null }) =>
      api<PromptTemplate>(`/prompts/${enc(id)}/duplicate`, { method: "POST", body }),
    onSuccess: invalidate,
  });
}

export function useImportPrompts() {
  const invalidate = useInvalidate(promptKeys.root);
  return useMutation({
    mutationFn: ({ bundle, onConflict }: { bundle: unknown; onConflict: "rename" | "skip" }) =>
      api<{ created: PromptTemplate[]; skipped: { name: string; reason: string }[] }>(
        "/prompts/import",
        { method: "POST", body: bundle, query: { on_conflict: onConflict } },
      ),
    onSuccess: invalidate,
  });
}

export function exportPrompts(ids: string[]) {
  return api<PromptExportBundle>("/prompts/export", {
    method: "POST",
    body: { template_ids: ids },
  });
}

export function useTestPrompt(id: string) {
  const invalidate = useInvalidate(queryKeys.runsRoot, promptKeys.root);
  return useMutation({
    mutationFn: (body: { inputs: Record<string, unknown>; version_id?: string | null }) =>
      api<{ run: Run; profile: { id: string; name: string } }>(`/prompts/${enc(id)}/test-run`, {
        method: "POST",
        body,
      }),
    onSuccess: invalidate,
  });
}

/** Save a JSON value as a file download in the browser. */
export function downloadJson(value: unknown, filename: string) {
  const blob = new Blob([JSON.stringify(value, null, 2) + "\n"], { type: "application/json" });
  saveBlob(blob, filename);
}
