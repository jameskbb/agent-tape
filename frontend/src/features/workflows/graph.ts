/** Pure DAG helpers shared by the editor, the graph, and the list view. */
import type { NodeType, WorkflowDefinition, WorkflowEdge, WorkflowNode } from "./api";

export const TYPE_LABELS: Record<NodeType, string> = {
  agent_run: "Agent run",
  command: "Command",
  transform: "Render / transform",
  evaluation_gate: "Evaluation gate",
  finding_review: "Finding review (challenge findings)",
  approval: "Human approval",
  condition: "Condition",
  collect: "Collect outputs",
};

export const TYPE_HINTS: Record<NodeType, string> = {
  agent_run: "Runs an agent profile as a child run.",
  command: "Runs one program with an argument array. Never a shell.",
  transform: "Renders text or reshapes earlier outputs.",
  evaluation_gate: "Runs evaluators; fails when a required one fails.",
  finding_review:
    "Runs a reviewer agent against the target node's findings after it finishes. The review " +
    "is model judgment and never changes the target's verdicts.",
  approval: "Pauses until someone approves in the inbox.",
  condition: "Follows the true or false branch.",
  collect: "Gathers outputs into the result and a JSON artifact.",
};

const KEY_RE = /^[A-Za-z][A-Za-z0-9_]{0,63}$/;

export function isValidKey(key: string): boolean {
  return KEY_RE.test(key);
}

/** Kahn's algorithm; `null` when the graph has a cycle. Ties keep declaration order. */
export function topoOrder(def: Pick<WorkflowDefinition, "nodes" | "edges">): string[] | null {
  const keys = def.nodes.map((n) => n.key);
  const indegree = new Map(keys.map((k) => [k, 0]));
  const children = new Map<string, string[]>(keys.map((k) => [k, []]));
  for (const e of def.edges) {
    if (!indegree.has(e.from) || !indegree.has(e.to)) continue;
    indegree.set(e.to, (indegree.get(e.to) ?? 0) + 1);
    children.get(e.from)?.push(e.to);
  }
  const ready = keys.filter((k) => indegree.get(k) === 0);
  const out: string[] = [];
  while (ready.length) {
    const k = ready.shift() as string;
    out.push(k);
    for (const c of children.get(k) ?? []) {
      const d = (indegree.get(c) ?? 0) - 1;
      indegree.set(c, d);
      if (d === 0) ready.push(c);
    }
  }
  return out.length === keys.length ? out : null;
}

/** Longest-path layer of each node (0 for roots). */
export function levels(def: Pick<WorkflowDefinition, "nodes" | "edges">): Map<string, number> {
  const order = topoOrder(def) ?? def.nodes.map((n) => n.key);
  const level = new Map<string, number>(order.map((k) => [k, 0]));
  for (const k of order) {
    for (const e of def.edges) {
      if (e.from === k && level.has(e.to)) {
        level.set(e.to, Math.max(level.get(e.to) ?? 0, (level.get(k) ?? 0) + 1));
      }
    }
  }
  return level;
}

/** Every node with a path of edges into `key` (a visited set keeps a cycle from looping). */
export function ancestors(
  def: Pick<WorkflowDefinition, "nodes" | "edges">,
  key: string,
): Set<string> {
  const seen = new Set<string>();
  const todo = def.edges.filter((e) => e.to === key).map((e) => e.from);
  while (todo.length) {
    const k = todo.pop() as string;
    if (seen.has(k)) continue;
    seen.add(k);
    todo.push(...def.edges.filter((e) => e.to === k).map((e) => e.from));
  }
  return seen;
}

export const COLUMN_GAP = 224;
export const ROW_GAP = 104;

/** Saved positions win; otherwise lay nodes out left-to-right by dependency layer. */
export function layout(
  def: Pick<WorkflowDefinition, "nodes" | "edges">,
): Map<string, { x: number; y: number }> {
  const lv = levels(def);
  const perLevel = new Map<number, number>();
  const out = new Map<string, { x: number; y: number }>();
  for (const n of def.nodes) {
    const l = lv.get(n.key) ?? 0;
    const row = perLevel.get(l) ?? 0;
    perLevel.set(l, row + 1);
    out.set(n.key, n.position ?? { x: l * COLUMN_GAP, y: row * ROW_GAP });
  }
  return out;
}

export function edgeLabel(edge: WorkflowEdge): string | undefined {
  const c = edge.condition;
  if (!c) return undefined;
  const parts: string[] = [];
  if (c.branch !== undefined) parts.push(c.branch ? "if true" : "if false");
  if (c.on && c.on !== "success") parts.push(`on ${c.on}`);
  if (c.when) parts.push(`when ${c.when}`);
  return parts.join(", ") || undefined;
}

export function uniqueKey(base: string, taken: Iterable<string>): string {
  const used = new Set(taken);
  const clean = base.replace(/[^A-Za-z0-9_]/g, "_").replace(/^[^A-Za-z]+/, "") || "node";
  if (!used.has(clean)) return clean;
  let i = 2;
  while (used.has(`${clean}_${i}`)) i += 1;
  return `${clean}_${i}`;
}

export function defaultConfig(type: NodeType): Record<string, unknown> {
  switch (type) {
    case "command":
      return { argv: ["git", "status", "--porcelain"] };
    case "agent_run":
      return { agent_profile_id: "", prompt: "" };
    case "transform":
      return { template: "" };
    case "evaluation_gate":
      return { evaluators: [{ type: "exit_code", name: "Exit code is 0" }] };
    case "finding_review":
      return { target: "", agent_profile_id: "" };
    case "approval":
      return { title: "Approve this step?" };
    case "condition":
      return { expression: "true" };
    case "collect":
      return {};
  }
}

const KEY_BASE: Partial<Record<NodeType, string>> = {
  agent_run: "agent",
  finding_review: "review",
};
const NAME_OVERRIDE: Partial<Record<NodeType, string>> = { finding_review: "Finding review" };

export function newNode(type: NodeType, taken: Iterable<string>): WorkflowNode {
  const key = uniqueKey(KEY_BASE[type] ?? type, taken);
  const name = NAME_OVERRIDE[type] ?? TYPE_LABELS[type];
  return { key, type, name, config: defaultConfig(type) };
}

/** Rename a node key everywhere it is referenced (edges and templates). */
export function renameKey(def: WorkflowDefinition, from: string, to: string): WorkflowDefinition {
  const re = new RegExp(`nodes\\.${from}\\b`, "g");
  const rewrite = (value: unknown): unknown => {
    if (typeof value === "string") return value.replace(re, `nodes.${to}`);
    if (Array.isArray(value)) return value.map(rewrite);
    if (value && typeof value === "object")
      return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, rewrite(v)]));
    return value;
  };
  return {
    ...def,
    nodes: def.nodes.map((n) => ({
      ...(rewrite(n) as WorkflowNode),
      key: n.key === from ? to : n.key,
    })),
    edges: def.edges.map((e) => ({
      ...e,
      from: e.from === from ? to : e.from,
      to: e.to === from ? to : e.to,
    })),
  };
}

export function emptyDefinition(): WorkflowDefinition {
  return {
    nodes: [newNode("command", [])],
    edges: [],
    inputs: [],
    settings: { max_parallel: 4 },
  };
}
