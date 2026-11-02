/** Pure helpers for the File Evidence views (tones, grouping, labels). */
import type { RunFile } from "./api";

export type Tone = "neutral" | "accent" | "success" | "danger" | "warning" | "violet";

const CLASSIFICATION_TONE: Record<string, Tone> = {
  created: "success",
  modified: "accent",
  renamed: "violet",
  deleted: "danger",
  outside_boundary: "danger",
  restored: "warning",
  net_unchanged: "warning",
};

export function classificationTone(file: Pick<RunFile, "classification" | "is_collapsed">): Tone {
  if (file.is_collapsed) return "neutral";
  return CLASSIFICATION_TONE[file.classification] ?? "neutral";
}

export function netStatusTone(status: string): Tone {
  const tones: Record<string, Tone | undefined> = {
    added: "success",
    modified: "accent",
    deleted: "danger",
    renamed: "violet",
  };
  return tones[status] ?? "neutral";
}

export function shortHash(hash: string | null | undefined): string {
  return hash ? hash.slice(0, 10) : "";
}

/** Parent directory of a repository-relative path ("." for top-level files). */
export function dirname(path: string): string {
  const trimmed = path.endsWith("/") ? path.slice(0, -1) : path;
  const i = trimmed.lastIndexOf("/");
  return i <= 0 ? "." : trimmed.slice(0, i) + "/";
}

export type GroupBy = "none" | "step" | "directory" | "classification" | "source";

export const GROUP_LABELS: Record<GroupBy, string> = {
  none: "No grouping",
  step: "Workflow step",
  directory: "Directory",
  classification: "Operation",
  source: "Evidence source",
};

/**
 * Group the current page of ledger rows. A file with several evidence sources appears under
 * each of them; every other grouping puts a file in exactly one group. Groups keep the order
 * in which they first appear, so the server's sort order is preserved.
 */
export function groupFiles(
  rows: RunFile[],
  by: GroupBy,
  labelSource: (source: string) => string = (s) => s,
): { key: string; label: string; rows: RunFile[] }[] {
  if (by === "none") return [{ key: "all", label: "", rows }];
  const groups = new Map<string, { key: string; label: string; rows: RunFile[] }>();
  const add = (key: string, label: string, row: RunFile) => {
    const group = groups.get(key) ?? { key, label, rows: [] };
    group.rows.push(row);
    groups.set(key, group);
  };
  for (const row of rows) {
    if (by === "step") {
      const step = row.first_step ?? row.last_step;
      add(step ?? "", step ?? "No workflow step", row);
    } else if (by === "directory") {
      const dir = row.is_collapsed ? row.file_key : dirname(row.file_key);
      add(dir, dir === "." ? "Repository root" : dir, row);
    } else if (by === "classification") {
      add(
        row.is_collapsed ? "collapsed" : row.classification,
        row.label || row.classification,
        row,
      );
    } else {
      const sources = row.sources.length ? row.sources : ["unknown"];
      for (const s of sources) add(s, labelSource(s), row);
    }
  }
  return [...groups.values()];
}

const SOURCE_LABELS: Record<string, string> = {
  agent_hook: "Agent report",
  tool_event: "Tool event",
  filesystem_watcher: "Filesystem watcher",
  git_start: "Git (start)",
  git_end: "Git (end)",
  command_reference: "Command arguments",
  reconciliation: "Reconciliation",
  manual: "Manual",
};

export function evidenceSourceLabel(source: string): string {
  return SOURCE_LABELS[source] ?? source.replace(/_/g, " ");
}

/** Protected-path wording for a write: approved by a reviewer, or only detected (never prevented). */
export function policyWording(policy: { decision?: string } | null | undefined): string {
  return policy?.decision === "approved" ? "approved" : "detected, not prevented";
}

const WRITE_OPS = ["create", "modify", "delete", "rename"];
const NET_CHANGES = ["added", "modified", "deleted", "renamed"];

/**
 * The badge for a protected file: a policy wording only when something was written (a
 * policy decision, a write operation, or a net change); a file that was only read or
 * searched is just a protected path.
 */
export function protectedLabel(
  file: Pick<RunFile, "policy" | "operation_counts" | "net_status">,
): string {
  const wrote =
    file.policy != null ||
    WRITE_OPS.some((op) => file.operation_counts[op]) ||
    NET_CHANGES.includes(file.net_status);
  return wrote ? `Protected, ${policyWording(file.policy)}` : "Protected path";
}
