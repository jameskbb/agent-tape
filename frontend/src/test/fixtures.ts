import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderResult } from "@testing-library/react";
import { createElement, type ReactElement } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, vi } from "vitest";

import type { RunDetail, RunEvent } from "@/api/types";
import type { AdapterDetails } from "@/features/agents/api";
import type { Citation, Finding, Verdict, VerdictValue } from "@/features/findings/api";

// `renderWithQuery`'s fetch stub lives for the whole test run of any file that imports it;
// restore it after each test so one test's stubbed API never leaks into the next.
afterEach(() => {
  vi.unstubAllGlobals();
});

/**
 * Renders a component inside a fresh `QueryClientProvider` (retries off) and a `MemoryRouter`,
 * with `globalThis.fetch` stubbed to answer `/api/v1/<key>` with the matching value in `api`
 * (404 for anything else). Component tests that call a hook built on `api()` (`useQuery`,
 * `useMutation`) need this; presentational tests do not.
 */
export function renderWithQuery(
  ui: ReactElement,
  { api = {} }: { api?: Record<string, unknown> } = {},
): RenderResult {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      const path = url.replace(/^.*\/api\/v1/, "").split("?")[0] ?? "";
      const match = Object.entries(api).find(([key]) => path.startsWith(key));
      if (!match) {
        return new Response(
          JSON.stringify({ error: { code: "not_found", message: "Not found" } }),
          {
            status: 404,
            headers: { "Content-Type": "application/json" },
          },
        );
      }
      return new Response(JSON.stringify(match[1]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
  return render(
    createElement(QueryClientProvider, { client }, createElement(MemoryRouter, null, ui)),
  );
}

let seq = 0;

export function makeEvent(overrides: Partial<RunEvent> = {}): RunEvent {
  seq += 1;
  return {
    id: `evt-${seq}`,
    run_id: "run-1",
    attempt_id: "att-1",
    step_run_id: null,
    node_key: null,
    parent_event_id: null,
    sequence: seq,
    ts: new Date(Date.UTC(2026, 0, 1, 12, 0, seq)).toISOString(),
    monotonic_ms: seq * 1000,
    type: "agent_message",
    source: "demo",
    severity: "info",
    summary: `event ${seq}`,
    payload: {},
    redaction: null,
    usage: null,
    cost: null,
    schema_version: 1,
    ...overrides,
  };
}

export function makeDetail(overrides: Partial<RunDetail["run"]> = {}): RunDetail {
  return {
    run: {
      id: "run-1",
      workspace_id: "ws",
      project_id: null,
      title: "Improve the greeting service",
      kind: "agent",
      source: "ui",
      source_ref: null,
      idempotency_key: null,
      status: "succeeded",
      failure_category: null,
      failure_message: null,
      priority: 0,
      adapter_type: "demo",
      prompt_template_id: null,
      prompt_version_id: null,
      agent_profile_id: null,
      agent_profile_version_id: null,
      workflow_id: null,
      workflow_version_id: null,
      schedule_id: null,
      trigger_id: null,
      parent_run_id: null,
      parent_relation: null,
      parent_node_key: null,
      tags: [],
      notes: null,
      max_attempts: 2,
      attempt_count: 2,
      queued_at: null,
      started_at: null,
      finished_at: null,
      duration_ms: 1200,
      cancel_requested_at: null,
      usage: null,
      cost: null,
      is_demo: true,
      created_at: new Date(Date.UTC(2026, 0, 1, 12, 0, 0)).toISOString(),
      updated_at: new Date(Date.UTC(2026, 0, 1, 12, 0, 0)).toISOString(),
      created_by: "local user",
      queue_position: null,
      prompt_name: null,
      agent_profile_name: "Demo Agent",
      project_name: null,
      findings_capture: null,
      ...overrides,
    },
    snapshot: {},
    inputs: {},
    rendered_prompt: null,
    outputs: null,
    options: {},
    attempts: [],
    steps: [],
    artifacts: [],
    file_changes: [],
    evaluations: [],
    approvals: [],
    children: [],
    event_count: 0,
    last_sequence: 0,
  };
}

/** A decided citation verdict (`lexical_v1`, no reason, empty evidence). */
export function makeVerdict(verdict: VerdictValue, overrides: Partial<Verdict> = {}): Verdict {
  seq += 1;
  return {
    id: `v-${seq}`,
    verdict,
    reason: null,
    method: "lexical_v1",
    verifier_version: "1",
    evidence: {},
    rerun_of_id: null,
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

/**
 * A file citation of `src/greeter.py` lines 1-2. `makeCitation("stale")` gives it a decided
 * verdict, `makeCitation(null)` (or no argument) leaves it undecided, and
 * `makeCitation({ ...overrides })` or `makeCitation("stale", { ...overrides })` sets any
 * other field.
 */
export function makeCitation(
  verdictOrOverrides: VerdictValue | null | Partial<Citation> = null,
  overrides: Partial<Citation> = {},
): Citation {
  const own =
    typeof verdictOrOverrides === "object" && verdictOrOverrides !== null ? verdictOrOverrides : {};
  const verdict = typeof verdictOrOverrides === "string" ? makeVerdict(verdictOrOverrides) : null;
  seq += 1;
  return {
    id: `c-${seq}`,
    position: seq,
    kind: "file",
    path: "src/greeter.py",
    inside_boundary: true,
    line_start: 1,
    line_end: 2,
    quote: "hello",
    event_ref: null,
    event_id: null,
    relation: null,
    details: {},
    verdict,
    checks: 1,
    reproduction: null,
    reproductions: 0,
    ...own,
    ...overrides,
  };
}

/**
 * A claim with a unique id and key, no linked sources, and no confirmation.
 * `makeFinding([citations])`, `makeFinding({ ...overrides })`, or
 * `makeFinding([citations], { ...overrides })`.
 */
export function makeFinding(
  citationsOrOverrides: Citation[] | Partial<Finding> = [],
  overrides: Partial<Finding> = {},
): Finding {
  const own = Array.isArray(citationsOrOverrides)
    ? { citations: citationsOrOverrides }
    : citationsOrOverrides;
  seq += 1;
  return {
    id: `f-${seq}`,
    key: `F${seq}`,
    kind: "claim",
    text: "greet() trims surrounding whitespace",
    source: null,
    event_id: null,
    ts: null,
    supports: [],
    conflicts: [],
    missing: [],
    citations: [],
    details: {},
    confirmation: null,
    ...own,
    ...overrides,
  };
}

/** The `demo` adapter's `GET /adapters` descriptor (scripted findings, ready). */
export function makeAdapter(overrides: Partial<AdapterDetails> = {}): AdapterDetails {
  return {
    name: "demo",
    display_name: "Demo Agent",
    description: "",
    capabilities: [],
    findings: "scripted",
    status: "ready",
    version: null,
    executable: null,
    message: null,
    details: {},
    config_schema: {},
    ...overrides,
  };
}
