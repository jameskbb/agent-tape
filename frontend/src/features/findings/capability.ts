/**
 * Whether a run's findings tab can show recorded findings, sourced from the backend (the
 * run's own `findings_capture`, falling back to the adapter registry's `GET /adapters`
 * descriptor, backend addition B5), never from a frontend list of adapter names that could
 * drift from the registry.
 */
import type { FindingsCapture, Run } from "@/api/types";
import type { AdapterDetails } from "@/features/agents/api";

type FindingsCapability = "observed" | "can" | "cannot";

const FINDINGS_RUN_KINDS: ReadonlySet<string> = new Set(["agent", "observed"]);

/** The run kinds that record findings: agent runs and observed (interactive) sessions. A
 * workflow run's findings live on its per-node child runs, so its findings surfaces (the
 * Findings and Context tabs, the header tally, the Challenge findings action) do not apply. */
export function recordsFindings(run: { kind: string }): boolean {
  return FINDINGS_RUN_KINDS.has(run.kind);
}

/** The one place the findings documentation link lives; the app has no in-app docs route,
 * so the empty state opens the repository docs in a new tab. */
export const FINDINGS_DOC_URL =
  "https://github.com/jameskbb/agent-tape/blob/main/docs/integrations/claude-code.md";

function descriptorFor(run: Run, adapters: AdapterDetails[] | undefined): AdapterDetails | null {
  if (!run.adapter_type || !adapters) return null;
  return adapters.find((adapter) => adapter.name === run.adapter_type) ?? null;
}

/** The run's own answer first: an adapter that hosts several agents (Herdr) cannot answer
 * for all of them at once, so its adapter-wide value is only the fallback. */
function captureFor(run: Run, adapters: AdapterDetails[] | undefined): FindingsCapture | null {
  if (run.findings_capture) return run.findings_capture;
  const adapterValue = descriptorFor(run, adapters)?.findings;
  return adapterValue && adapterValue !== "per_profile" ? adapterValue : null;
}

/** `run.kind === "observed"` is always "observed". Otherwise a run (or, failing that, an
 * adapter) whose capture is `"unsupported"` is "cannot"; an unknown capture never claims
 * "cannot", it is "can". */
export function findingsCapability(
  run: Run,
  adapters: AdapterDetails[] | undefined,
): FindingsCapability {
  if (run.kind === "observed") return "observed";
  return captureFor(run, adapters) === "unsupported" ? "cannot" : "can";
}

/** What the "cannot record" empty state names: the adapter's display name, falling back to
 * the raw adapter type, then a generic label, when the descriptor has not loaded yet. When
 * the adapter hosts several agents and only some can record findings, the adapter alone is
 * not the reason, so the run's own profile is named alongside it. */
export function adapterLabel(run: Run, adapters: AdapterDetails[] | undefined): string {
  const descriptor = descriptorFor(run, adapters);
  const adapterName = descriptor?.display_name ?? run.adapter_type ?? "This adapter";
  if (descriptor?.findings === "per_profile" && run.agent_profile_name) {
    return `${run.agent_profile_name} (${adapterName})`;
  }
  return adapterName;
}
