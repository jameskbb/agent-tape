import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";

import type { RunDetail } from "@/api/types";
import { useAdapterDetails } from "@/features/agents/api";

import {
  makeAdapter,
  makeCitation,
  makeDetail,
  makeFinding,
  renderWithQuery,
} from "@/test/fixtures";

import { useFindings } from "./api";
import { RunHeaderTally } from "./RunHeaderTally";

/** Reads the same two queries as the tally, from the same cache, and says when both have
 * settled. The tally renders nothing while loading, so an absence assertion only means
 * something once this probe reports the loaded (or failed) state. */
function SettledProbe({ runId }: { runId: string }) {
  const findings = useFindings(runId, false);
  const adapters = useAdapterDetails();
  if (findings.isError) return <p>findings failed</p>;
  if (findings.isSuccess && adapters.isSuccess) return <p>findings loaded</p>;
  return null;
}

function TallyWithProbe({ detail }: { detail: RunDetail }) {
  return (
    <>
      <dl data-testid="tally">
        <RunHeaderTally detail={detail} />
      </dl>
      <SettledProbe runId={detail.run.id} />
    </>
  );
}

describe("RunHeaderTally", () => {
  it("renders '{total}: {tally phrases}' linking to ?tab=findings for the seeded-demo shape", async () => {
    const findings = [
      makeFinding([makeCitation("quote_matches")], { key: "F1" }),
      makeFinding([makeCitation("quote_matches")], { key: "F2" }),
      makeFinding([makeCitation("quote_matches")], { key: "F3" }),
      makeFinding([makeCitation("stale")], { key: "F4" }),
      makeFinding([makeCitation("quote_not_found")], { key: "F5" }),
      makeFinding([makeCitation("never_read")], { key: "F6" }),
      makeFinding([makeCitation("read_not_observed")], { key: "F7" }),
      makeFinding([], { key: "F8" }),
    ];
    const detail = makeDetail({ status: "succeeded", adapter_type: "demo" });
    renderWithQuery(<RunHeaderTally detail={detail} />, {
      api: {
        "/runs/run-1/context/findings": findings,
        "/adapters": [makeAdapter()],
      },
    });
    expect(await screen.findByText("Findings")).toBeInTheDocument();
    const link = await screen.findByRole("link", {
      name: "8: 3 quotes match, 1 stale, 1 quote not found, 1 never read, 1 read not observed, 1 declared only",
    });
    expect(link).toHaveAttribute("href", expect.stringContaining("tab=findings"));
  });

  it("shows '0' for a demo-adapter run with no findings", async () => {
    const detail = makeDetail({ status: "succeeded", adapter_type: "demo" });
    renderWithQuery(<RunHeaderTally detail={detail} />, {
      api: {
        "/runs/run-1/context/findings": [],
        "/adapters": [makeAdapter()],
      },
    });
    expect(await screen.findByRole("link", { name: "0" })).toBeInTheDocument();
  });

  it("renders nothing for a codex-cli run that cannot record findings", async () => {
    const detail = makeDetail({ status: "succeeded", adapter_type: "codex-cli" });
    renderWithQuery(<TallyWithProbe detail={detail} />, {
      api: {
        "/runs/run-1/context/findings": [],
        "/adapters": [
          makeAdapter({ name: "codex-cli", display_name: "Codex CLI", findings: "unsupported" }),
        ],
      },
    });
    await screen.findByText("findings loaded");
    expect(screen.getByTestId("tally")).toBeEmptyDOMElement();
  });

  it("renders nothing for a workflow run, even with findings present", async () => {
    const detail = makeDetail({ status: "succeeded", kind: "workflow", adapter_type: "demo" });
    renderWithQuery(<TallyWithProbe detail={detail} />, {
      api: {
        "/runs/run-1/context/findings": [makeFinding([makeCitation("quote_matches")])],
        "/adapters": [makeAdapter()],
      },
    });
    await screen.findByText("findings loaded");
    expect(screen.getByTestId("tally")).toBeEmptyDOMElement();
  });

  it("renders nothing while loading", () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise(() => {})),
    );
    const detail = makeDetail({ status: "succeeded", adapter_type: "demo" });
    const { container } = render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <RunHeaderTally detail={detail} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(container.textContent).toBe("");
    vi.unstubAllGlobals();
  });

  it("renders nothing on a fetch error", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(JSON.stringify({ error: { code: "server_error", message: "boom" } }), {
            status: 500,
            headers: { "Content-Type": "application/json" },
          }),
        ),
      ),
    );
    const detail = makeDetail({ status: "succeeded", adapter_type: "demo" });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <TallyWithProbe detail={detail} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await screen.findByText("findings failed");
    expect(screen.getByTestId("tally")).toBeEmptyDOMElement();
    vi.unstubAllGlobals();
  });

  it("keeps other query params when linking to the Findings tab", async () => {
    const detail = makeDetail({ status: "succeeded", adapter_type: "demo" });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(JSON.stringify([]), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        ),
      ),
    );
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/runs/run-1?tab=timeline&foo=bar"]}>
          <RunHeaderTally detail={detail} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const link = await screen.findByRole("link", { name: "0" });
    const href = link.getAttribute("href") ?? "";
    expect(href).toContain("tab=findings");
    expect(href).toContain("foo=bar");
    vi.unstubAllGlobals();
  });
});
