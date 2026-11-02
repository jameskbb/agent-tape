/**
 * Wording for context stages. The language is deliberately narrow: AgentTape can show what
 * was supplied to the agent, retrieved by the agent, and cited in its output. It never says a
 * model "used", "reasoned from", or "paid attention to" anything.
 */
import type { Classification, Destination, Stage } from "./api";

type Tone = "neutral" | "accent" | "live" | "success" | "danger" | "warning" | "violet";

export const STAGE_INFO: Record<Stage, { label: string; tone: Tone; help: string }> = {
  available: {
    label: "Available",
    tone: "neutral",
    help: "Accessible to the run, but not placed in the context pack",
  },
  selected: {
    label: "Selected",
    tone: "accent",
    help: "Placed in the context pack by AgentTape (a reference with a hash, not a copy)",
  },
  retrieved: {
    label: "Retrieved",
    tone: "live",
    help: "The agent searched, opened, read, or queried it, as reported by the adapter or agent",
  },
  injected: {
    label: "Injected",
    tone: "violet",
    help: "AgentTape itself placed this text into the input it handed to the agent",
  },
  referenced: {
    label: "Referenced",
    tone: "neutral",
    help: "Mentioned or linked in the agent's structured output",
  },
  cited: {
    label: "Cited",
    tone: "success",
    help: "A finding, recommendation, or decision points to it as evidence",
  },
  unavailable: {
    label: "Unavailable",
    tone: "danger",
    help: "The run tried to use it, but access or capability was missing",
  },
  unknown: {
    label: "Unknown",
    tone: "neutral",
    help: "The adapter cannot tell whether the provider included it",
  },
  excluded: { label: "Excluded", tone: "neutral", help: "Considered but left out, with a reason" },
  conflict: { label: "Conflict", tone: "danger", help: "Sources disagree" },
  stale: { label: "Stale", tone: "warning", help: "Known to be out of date" },
  gap: { label: "Gap", tone: "warning", help: "Context that was needed but missing" },
};

export const STAGE_ORDER: Stage[] = Object.keys(STAGE_INFO) as Stage[];

export const QUALITY_LABEL: Record<string, string> = {
  assembled: "Assembled by AgentTape",
  "adapter-observed": "Observed in the adapter's tool stream",
  "agent-declared": "Declared by the agent",
  "user-reported": "Reported by a person",
};

export const CLASSIFICATIONS: { value: Classification; label: string }[] = [
  { value: "run-specific", label: "Run-specific (no lasting change)" },
  { value: "source-correction", label: "Source correction" },
  { value: "business-knowledge", label: "Durable business knowledge" },
  { value: "agent-behavior", label: "Agent behavior guidance" },
];

export const DESTINATIONS: { value: Destination; label: string; injects: boolean }[] = [
  {
    value: "project_instruction",
    label: "Project instruction (injected into future runs)",
    injects: true,
  },
  {
    value: "prompt_revision",
    label: "Prompt revision (injected until the template is revised)",
    injects: true,
  },
  {
    value: "knowledge_source_update",
    label: "Knowledge source update (fix it at the source)",
    injects: false,
  },
  {
    value: "context_provider_rule",
    label: "Context-provider rule (applied outside AgentTape)",
    injects: false,
  },
  { value: "no_promotion", label: "No promotion", injects: false },
];

export function destinationLabel(value: string | null): string {
  return DESTINATIONS.find((d) => d.value === value)?.label ?? "Not proposed yet";
}

export function freshnessLabel(row: {
  freshness: string;
  live: boolean | null;
  synced_at: string | null;
  version: string | null;
}): string {
  if (row.freshness === "stale") return "Stale";
  if (row.freshness === "live") return "Live at query time";
  if (row.freshness === "snapshot") {
    const parts = ["Snapshot"];
    if (row.synced_at) parts.push(`synced ${row.synced_at}`);
    if (row.version) parts.push(`version ${row.version}`);
    return parts.join(", ");
  }
  return "Freshness unknown";
}

export function shortHash(hash: string | null | undefined): string {
  return hash ? hash.slice(0, 12) : "none";
}

export function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

/** Full commit ids are shortened for display; other version labels are kept as reported. */
export function shortVersion(version: string | null | undefined): string | null {
  if (!version) return null;
  return /^[0-9a-f]{40,64}$/.test(version) ? version.slice(0, 10) : version;
}
