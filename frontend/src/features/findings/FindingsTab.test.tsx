import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";

import type { FindingsCapture, RunStatus } from "@/api/types";
import { ToastProvider } from "@/components/ui";
import type { AdapterDetails } from "@/features/agents/api";
import {
  makeAdapter,
  makeCitation,
  makeDetail,
  makeFinding,
  renderWithQuery,
} from "@/test/fixtures";

import type { Finding } from "./api";
import { FindingsTab, FindingsTabCount } from "./FindingsTab";

function wrap(ui: ReactElement) {
  return <ToastProvider>{ui}</ToastProvider>;
}

function renderTab(options: {
  findings: Finding[];
  status?: RunStatus;
  finishedAt?: string | null;
  live?: boolean;
  fileSummary?: Record<string, unknown>;
  kind?: string;
  adapterType?: string | null;
  adapters?: AdapterDetails[];
  findingsCapture?: FindingsCapture | null;
  agentProfileName?: string | null;
}) {
  const detail = makeDetail({
    status: options.status ?? "succeeded",
    finished_at: options.finishedAt ?? null,
    kind: options.kind ?? "agent",
    adapter_type: options.adapterType ?? "demo",
    findings_capture: options.findingsCapture ?? null,
    agent_profile_name: options.agentProfileName ?? "Demo Agent",
  });
  const api: Record<string, unknown> = {
    "/runs/run-1/context/findings": options.findings,
    "/adapters": options.adapters ?? [makeAdapter()],
  };
  if (options.fileSummary) {
    api["/runs/run-1/files/summary"] = options.fileSummary;
  }
  return renderWithQuery(
    wrap(<FindingsTab detail={detail} events={[]} live={options.live ?? false} />),
    { api },
  );
}

describe("FindingsTab statement", () => {
  it("shows the terminal statement with the tally and the caveat", async () => {
    const findings = [
      makeFinding([makeCitation("quote_matches")], {
        key: "F1",
        text: "greet() trims surrounding whitespace",
      }),
      makeFinding([makeCitation("stale")], { key: "F2", text: "The change is ready to ship" }),
    ];
    renderTab({ findings });
    expect(
      await screen.findByText(/AgentTape checked 2 citations in 2 findings/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Checkable, not foolproof: a matching quote shows the lines existed/),
    ).toBeInTheDocument();
  });

  it("shows the live statement", async () => {
    const findings = [
      makeFinding([makeCitation("quote_matches")], { key: "F1", text: "greet() trims whitespace" }),
    ];
    renderTab({ findings, status: "running", live: true });
    expect(
      await screen.findByText(
        "This run is still recording. Citations are checked once it finishes.",
      ),
    ).toBeInTheDocument();
  });

  it("shows the all-declared-only statement", async () => {
    const findings = [makeFinding([], { key: "F1", text: "A recommendation with no citation" })];
    renderTab({ findings });
    expect(
      await screen.findByText(
        "This run recorded 1 findings and none of them cites lines or a recorded event, so there is nothing to check.",
      ),
    ).toBeInTheDocument();
  });

  it("appends the file-evidence completeness sentence when it is not complete", async () => {
    const findings = [
      makeFinding([makeCitation("quote_matches")], { key: "F1", text: "greet() trims whitespace" }),
    ];
    renderTab({
      findings,
      fileSummary: {
        completeness: {
          level: "partial",
          statement: "AgentTape was offline from +00:12.0 to +00:41.3.",
        },
      },
    });
    expect(
      await screen.findByText(/AgentTape checked 1 citations in 1 findings/),
    ).toBeInTheDocument();
    expect(
      await screen.findByText(/AgentTape was offline from \+00:12\.0 to \+00:41\.3\./),
    ).toBeInTheDocument();
  });
});

describe("FindingsTab tally chips", () => {
  it("filters the list by a pressed chip, shows the caption and Clear filter, and disables a zero chip", async () => {
    const findings = [
      makeFinding([makeCitation("quote_matches")], { key: "F1", text: "greet() trims whitespace" }),
      makeFinding([makeCitation("stale")], { key: "F2", text: "The change is ready to ship" }),
    ];
    renderTab({ findings });
    const staleChip = await screen.findByRole("button", { name: "Stale 1" });
    const neverReadChip = screen.getByRole("button", { name: "Never read 0" });
    expect(neverReadChip).toBeDisabled();

    await userEvent.click(staleChip);
    expect(staleChip).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("Showing 1 of 2 findings with Stale")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear filter" })).toBeInTheDocument();
    expect(screen.queryByText("greet() trims whitespace")).not.toBeInTheDocument();
  });
});

describe("FindingsTabCount", () => {
  it("renders nothing while loading, then the finding count", async () => {
    const detail = makeDetail({ status: "succeeded" });
    const findings = [
      makeFinding([makeCitation("quote_matches")], { key: "F1", text: "greet() trims whitespace" }),
    ];
    renderWithQuery(<FindingsTabCount detail={detail} />, {
      api: { "/runs/run-1/context/findings": findings },
    });
    expect(screen.queryByText("1")).not.toBeInTheDocument();
    expect(await screen.findByText("1")).toBeInTheDocument();
  });
});

describe("FindingsTab Re-check citations", () => {
  it("is disabled while the run is live, with its title", async () => {
    const findings = [
      makeFinding([makeCitation("quote_matches")], { key: "F1", text: "greet() trims whitespace" }),
    ];
    renderTab({ findings, status: "running", live: true });
    const button = await screen.findByRole("button", { name: /Re-check citations/ });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("title", "Citations are checked once the run finishes");
  });

  it("toasts the recorded verdict count on success", async () => {
    const findings = [
      makeFinding([makeCitation("quote_matches")], { key: "F1", text: "greet() trims whitespace" }),
    ];
    renderTab({ findings });
    const button = await screen.findByRole("button", { name: /Re-check citations/ });
    // renderTab's fetch stub answers every method the same way (200 with the mapped body).
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url =
          typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
        if (url.includes("/findings/verify")) {
          return Promise.resolve(
            new Response(
              JSON.stringify({
                run_id: "run-1",
                citations: 1,
                recorded: 1,
                unchanged: 0,
                verdicts: {},
              }),
              {
                status: 200,
                headers: { "Content-Type": "application/json" },
              },
            ),
          );
        }
        return Promise.resolve(
          new Response(JSON.stringify(findings), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }),
    );
    await userEvent.click(button);
    expect(
      await screen.findByText("Citations re-checked: 1 verdicts recorded"),
    ).toBeInTheDocument();
  });

  it("toasts the 409 message verbatim", async () => {
    const findings = [
      makeFinding([makeCitation("quote_matches")], { key: "F1", text: "greet() trims whitespace" }),
    ];
    renderTab({ findings });
    const button = await screen.findByRole("button", { name: /Re-check citations/ });
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url =
          typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
        if (url.includes("/findings/verify")) {
          return Promise.resolve(
            new Response(
              JSON.stringify({
                error: {
                  code: "run_not_finished",
                  message: "Citations are verified once a run has finished; this run is running",
                },
              }),
              { status: 409, headers: { "Content-Type": "application/json" } },
            ),
          );
        }
        return Promise.resolve(
          new Response(JSON.stringify(findings), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }),
    );
    await userEvent.click(button);
    expect(
      await screen.findByText(
        "Citations are verified once a run has finished; this run is running",
      ),
    ).toBeInTheDocument();
  });
});

describe("FindingsTab empty states", () => {
  it("shows the adapter-cannot-record empty state, naming the adapter, with a docs link, and no count chip", async () => {
    renderTab({
      findings: [],
      adapterType: "codex-cli",
      adapters: [
        makeAdapter({ name: "codex-cli", display_name: "Codex CLI", findings: "unsupported" }),
      ],
    });
    expect(await screen.findByText("No findings recorded")).toBeInTheDocument();
    expect(screen.getByText(/^Codex CLI does not record findings\./)).toBeInTheDocument();
    expect(screen.getByText("record_finding")).toBeInTheDocument();
    expect(screen.getByText("agenttape mcp register")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "How findings are recorded" });
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noreferrer");
    expect(screen.queryByRole("group", { name: "Filter by verdict" })).not.toBeInTheDocument();
  });

  it("shows the adapter-can-record, none-recorded empty state", async () => {
    renderTab({
      findings: [],
      adapterType: "demo",
      adapters: [makeAdapter({ name: "demo", display_name: "Demo Agent", findings: "scripted" })],
    });
    expect(await screen.findByText("This run recorded no findings.")).toBeInTheDocument();
    expect(
      screen.getByText(
        "The agent records a finding with the record_finding tool; nothing was recorded during this run.",
      ),
    ).toBeInTheDocument();
  });

  it("shows the observed-session, none-recorded empty state", async () => {
    renderTab({ findings: [], kind: "observed", adapterType: "claude-code" });
    expect(await screen.findByText("This session recorded no findings.")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Findings from an interactive Claude Code session arrive through the AgentTape MCP server (agenttape mcp register).",
      ),
    ).toBeInTheDocument();
  });

  it("renders the normal tab, not an empty state, when a cannot-record adapter's run has findings", async () => {
    const findings = [
      makeFinding([makeCitation("quote_matches")], { key: "F1", text: "greet() trims whitespace" }),
    ];
    renderTab({
      findings,
      adapterType: "codex-cli",
      adapters: [
        makeAdapter({ name: "codex-cli", display_name: "Codex CLI", findings: "unsupported" }),
      ],
    });
    expect(await screen.findByRole("group", { name: "Filter by verdict" })).toBeInTheDocument();
    expect(screen.queryByText("No findings recorded")).not.toBeInTheDocument();
  });
});

describe("FindingsTabCount empty states", () => {
  // Renders FindingsTabCount alongside FindingsTab so the same query cache settles both;
  // FindingsTab's rendered empty-state text is the deterministic "query has resolved" signal
  // (FindingsTabCount alone renders nothing both while loading and once resolved-to-hidden).
  it("renders nothing when the adapter cannot record and none were found", async () => {
    const detail = makeDetail({ status: "succeeded", adapter_type: "codex-cli" });
    renderWithQuery(
      wrap(
        <>
          <FindingsTabCount detail={detail} />
          <FindingsTab detail={detail} events={[]} live={false} />
        </>,
      ),
      {
        api: {
          "/runs/run-1/context/findings": [],
          "/adapters": [
            makeAdapter({ name: "codex-cli", display_name: "Codex CLI", findings: "unsupported" }),
          ],
        },
      },
    );
    expect(await screen.findByText("No findings recorded")).toBeInTheDocument();
    expect(screen.queryByText("0")).not.toBeInTheDocument();
  });

  it("renders 0 when the adapter can record and none were found", async () => {
    const detail = makeDetail({ status: "succeeded", adapter_type: "demo" });
    renderWithQuery(
      wrap(
        <>
          <FindingsTabCount detail={detail} />
          <FindingsTab detail={detail} events={[]} live={false} />
        </>,
      ),
      {
        api: {
          "/runs/run-1/context/findings": [],
          "/adapters": [makeAdapter({ name: "demo", findings: "scripted" })],
        },
      },
    );
    expect(await screen.findByText("This run recorded no findings.")).toBeInTheDocument();
    expect(await screen.findByText("0")).toBeInTheDocument();
  });
});

describe("FindingsTab list, picker, and detail", () => {
  it("renders the list in the response order, never sorted by verdict", async () => {
    const findings = [
      makeFinding([makeCitation("quote_not_found")], {
        key: "F1",
        text: "This claim has a worse verdict but comes first",
      }),
      makeFinding([makeCitation("quote_matches")], {
        key: "F2",
        text: "This claim quote matches and comes second",
      }),
    ];
    renderTab({ findings });
    const list = await screen.findByRole("list", { name: "Findings" });
    const rows = within(list).getAllByRole("button");
    expect(rows.map((row) => row.textContent)).toEqual([
      expect.stringContaining("This claim has a worse verdict but comes first"),
      expect.stringContaining("This claim quote matches and comes second"),
    ]);
  });

  it("moves the roving-tabindex selection, and the detail pane, with ArrowDown", async () => {
    const findings = [
      makeFinding([makeCitation("quote_matches")], {
        key: "F1",
        text: "First claim about the greeting service",
      }),
      makeFinding([makeCitation("stale")], {
        key: "F2",
        text: "Second claim about whitespace handling",
      }),
    ];
    renderTab({ findings });
    const list = await screen.findByRole("list", { name: "Findings" });
    const rows = within(list).getAllByRole("button");
    const first = rows[0];
    if (!first) throw new Error("expected at least one row");
    first.focus();
    await userEvent.keyboard("{ArrowDown}");
    expect(
      await screen.findByRole("heading", {
        level: 3,
        name: "Second claim about whitespace handling",
      }),
    ).toBeInTheDocument();
  });

  it("shows the stacked picker option 'Finding {i} of {n}: {claim}'", async () => {
    const findings = [
      makeFinding([makeCitation("quote_matches")], { key: "F1", text: "First claim" }),
      makeFinding([makeCitation("stale")], {
        key: "F2",
        text: "Second claim about the greeting service",
      }),
    ];
    renderTab({ findings });
    const picker = await screen.findByRole("combobox", { name: "Findings" });
    expect(
      within(picker).getByRole("option", {
        name: "Finding 2 of 2: Second claim about the greeting service",
      }),
    ).toBeInTheDocument();
  });

  it("shows the full claim, the partial-payload notes, and the declared-only sentence", async () => {
    const findings = [
      makeFinding([makeCitation("quote_matches")], {
        key: "F1",
        text: "A finding whose payload was capped",
        details: { citations_dropped: 2, payload_truncated: true },
      }),
      makeFinding([], { key: "F2", text: "A finding declared with no citation" }),
    ];
    renderTab({ findings });
    expect(
      await screen.findByRole("heading", { level: 3, name: "A finding whose payload was capped" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("2 citations past the 50-citation cap were not recorded"),
    ).toBeInTheDocument();
    expect(screen.getByText("The finding payload was cut at its size cap")).toBeInTheDocument();

    await userEvent.click(
      screen.getByRole("button", { name: /A finding declared with no citation/ }),
    );
    expect(
      screen.getByText(
        "No citation given. AgentTape can only check a claim that cites lines or a recorded event.",
      ),
    ).toBeInTheDocument();
  });
});
