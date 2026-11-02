/** Timeline grouping and filtering (pure; unit tested). */
import type { RunEvent, Severity } from "@/api/types";

export interface EventGroup {
  id: string;
  label: string;
  types: string[];
}

export const EVENT_GROUPS: EventGroup[] = [
  { id: "agent", label: "Agent", types: ["agent_message", "reasoning_summary"] },
  { id: "tools", label: "Tools", types: ["tool_call", "tool_result"] },
  { id: "commands", label: "Commands", types: ["command"] },
  { id: "output", label: "Output", types: ["stdout", "stderr"] },
  { id: "files", label: "Files", types: ["file_read", "file_write", "git_change", "artifact"] },
  {
    id: "lifecycle",
    label: "Lifecycle",
    types: ["lifecycle", "retry", "timeout", "cancellation", "step", "scheduler", "trigger"],
  },
  {
    id: "approvals",
    label: "Approvals",
    types: ["approval_requested", "approval_granted", "approval_rejected", "approval_expired"],
  },
  { id: "evaluations", label: "Evaluations", types: ["evaluation"] },
  { id: "usage", label: "Usage", types: ["usage"] },
  { id: "problems", label: "Problems", types: ["error", "warning"] },
];

const GROUP_OF = new Map<string, string>(
  EVENT_GROUPS.flatMap((g) => g.types.map((t) => [t, g.id] as const)),
);

export function groupOf(type: string): string {
  return GROUP_OF.get(type) ?? "lifecycle";
}

const SEVERITY_RANK: Record<Severity, number> = {
  debug: 0,
  info: 1,
  notice: 2,
  warning: 3,
  error: 4,
  critical: 5,
};

export type MinSeverity = "all" | "notice" | "warning" | "error";

export interface TimelineFilters {
  /** Selected group ids; empty means all groups. */
  groups: string[];
  minSeverity: MinSeverity;
  attemptId: string | null;
  nodeKey: string | null;
  text: string;
}

export const EMPTY_FILTERS: TimelineFilters = {
  groups: [],
  minSeverity: "all",
  attemptId: null,
  nodeKey: null,
  text: "",
};

function searchable(event: RunEvent): string {
  const payload = event.payload;
  const text = typeof payload.text === "string" ? payload.text : "";
  const output = typeof payload.output === "string" ? payload.output : "";
  return `${event.summary}\n${text}\n${output}\n${event.type}`.toLowerCase();
}

export function filterEvents(events: RunEvent[], filters: TimelineFilters): RunEvent[] {
  const groups = new Set(filters.groups);
  const needle = filters.text.trim().toLowerCase();
  const min = filters.minSeverity === "all" ? 0 : SEVERITY_RANK[filters.minSeverity];
  return events.filter((e) => {
    if (groups.size > 0 && !groups.has(groupOf(e.type))) return false;
    if (SEVERITY_RANK[e.severity] < min) return false;
    if (filters.attemptId && e.attempt_id !== filters.attemptId) return false;
    if (filters.nodeKey && e.node_key !== filters.nodeKey) return false;
    if (needle && !searchable(e).includes(needle)) return false;
    return true;
  });
}

export function countByGroup(events: RunEvent[]): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const e of events) {
    const g = groupOf(e.type);
    counts[g] = (counts[g] ?? 0) + 1;
  }
  return counts;
}

/** Merge new events into a sorted, de-duplicated list (by sequence). */
export function mergeEvents(existing: RunEvent[], incoming: RunEvent[]): RunEvent[] {
  if (incoming.length === 0) return existing;
  const last = existing.at(-1)?.sequence ?? 0;
  if (incoming.every((e) => e.sequence > last) && isSorted(incoming))
    return [...existing, ...incoming];
  const bySeq = new Map<number, RunEvent>();
  for (const e of existing) bySeq.set(e.sequence, e);
  for (const e of incoming) bySeq.set(e.sequence, e);
  return [...bySeq.values()].sort((a, b) => a.sequence - b.sequence);
}

function isSorted(events: RunEvent[]): boolean {
  for (let i = 1; i < events.length; i += 1) {
    const prev = events[i - 1];
    const cur = events[i];
    if (prev && cur && cur.sequence <= prev.sequence) return false;
  }
  return true;
}
