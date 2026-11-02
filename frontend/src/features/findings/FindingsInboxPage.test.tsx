import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation, useParams } from "react-router";
import { describe, expect, it, vi } from "vitest";

import { renderWithQuery } from "@/test/fixtures";

import type { FindingRow } from "./api";
import { FindingsInboxPage } from "./FindingsInboxPage";

let seq = 0;

function makeRow(overrides: Partial<FindingRow> = {}): FindingRow {
  seq += 1;
  const id = overrides.id ?? `finding-${seq}`;
  return {
    id,
    key: `F${seq}`,
    kind: "claim",
    text: `Claim number ${seq}`,
    created_at: "2026-01-01T00:00:00Z",
    citations: 1,
    verdict_counts: { quote_matches: 1 },
    worst_verdict: "quote_matches",
    run: {
      id: `run-${seq}`,
      title: `Improve the greeting service ${seq}`,
      status: "succeeded",
      kind: "agent",
      agent_label: "Demo Agent",
      project_id: null,
      project_name: null,
    },
    ...overrides,
  };
}

function page(rows: FindingRow[], total = rows.length) {
  return { items: rows, total, limit: 50, offset: 0, next_offset: null };
}

function fetchUrl(input: RequestInfo | URL | undefined): string {
  if (input === undefined) return "";
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.href;
  return input.url;
}

function lastFetchUrl(): string {
  return fetchUrl(vi.mocked(globalThis.fetch).mock.calls.at(-1)?.[0]);
}

function RunStub() {
  const { runId } = useParams();
  const location = useLocation();
  return (
    <p>
      run: {runId} {location.search}
    </p>
  );
}

function renderInbox(api: Record<string, unknown>) {
  return renderWithQuery(
    <Routes>
      <Route path="/" element={<FindingsInboxPage />} />
      <Route path="/runs/:runId" element={<RunStub />} />
    </Routes>,
    { api },
  );
}

describe("FindingsInboxPage", () => {
  it("requests the default window and renders rows in response order", async () => {
    const rows = [
      makeRow({ text: "First claim", worst_verdict: "stale" }),
      makeRow({ text: "Second claim" }),
    ];
    renderInbox({ "/findings": page(rows) });
    expect(await screen.findByText("First claim")).toBeInTheDocument();
    const rendered = screen.getAllByRole("link", { name: /claim/ });
    expect(rendered.map((r) => r.textContent)).toEqual(["First claim", "Second claim"]);
    expect(screen.getAllByRole("cell", { name: "Demo Agent" })).toHaveLength(2);
    expect(screen.getAllByRole("cell", { name: "Launched run" })).toHaveLength(2);
    const url = fetchUrl(vi.mocked(globalThis.fetch).mock.calls[0]?.[0]);
    expect(url).toContain("days=14");
    expect(url).toContain("sort=flagged");
    expect(url).toContain("limit=50");
    expect(url).toContain("offset=0");
  });

  it("shows the Findings across runs caption and page title", async () => {
    renderInbox({ "/findings": page([makeRow()]) });
    expect(await screen.findByRole("heading", { name: "Findings" })).toBeInTheDocument();
    expect(await screen.findByText("Findings across runs")).toBeInTheDocument();
  });

  it("switches to a 7-day window and refetches", async () => {
    renderInbox({ "/findings": page([makeRow()]) });
    await screen.findByText(/Claim number/);
    const before = vi.mocked(globalThis.fetch).mock.calls.length;
    await userEvent.click(screen.getByRole("radio", { name: "7 days" }));
    await waitFor(() =>
      expect(vi.mocked(globalThis.fetch).mock.calls.length).toBeGreaterThan(before),
    );
    expect(lastFetchUrl()).toContain("days=7");
  });

  it("clicking a row navigates to the run's findings tab with the finding selected", async () => {
    const rows = [makeRow({ key: "F9", text: "Row click claim" })];
    renderInbox({ "/findings": page(rows) });
    const row = (await screen.findByText("Row click claim")).closest("tr");
    if (!row) throw new Error("row not found");
    await userEvent.click(row);
    expect(await screen.findByText(/run:/)).toHaveTextContent(/tab=findings&finding=F9/);
  });

  it("clicking the claim link itself also navigates there (keyboard users)", async () => {
    const rows = [makeRow({ key: "F10", text: "Keyboard claim" })];
    renderInbox({ "/findings": page(rows) });
    const link = await screen.findByRole("link", { name: "Keyboard claim" });
    expect(link).toHaveAttribute("href", expect.stringContaining("tab=findings&finding=F10"));
  });

  it("reads the citations cell as '{n} citations, {flagged phrase}'", async () => {
    const rows = [
      makeRow({
        text: "Flagged claim",
        citations: 2,
        verdict_counts: { quote_matches: 1, stale: 1 },
        worst_verdict: "stale",
      }),
    ];
    renderInbox({ "/findings": page(rows) });
    expect(await screen.findByText("2 citations, 1 stale")).toBeInTheDocument();
  });

  it("reads a single unflagged citation as '1 citation'", async () => {
    const rows = [makeRow({ text: "Clean claim", citations: 1 })];
    renderInbox({ "/findings": page(rows) });
    expect(await screen.findByText("1 citation")).toBeInTheDocument();
  });

  it("reads zero citations as 'No citation'", async () => {
    const rows = [
      makeRow({
        text: "Declared claim",
        citations: 0,
        verdict_counts: {},
        worst_verdict: "declared_only",
      }),
    ];
    renderInbox({ "/findings": page(rows) });
    expect(await screen.findByText("No citation")).toBeInTheDocument();
  });

  it("shows a loading state before data arrives", () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise(() => {})),
    );
    const { container } = renderWithQuery(<FindingsInboxPage />, { api: {} });
    expect(container.textContent).not.toContain("Findings across runs");
  });

  it("renders ErrorState with a retry action on a failed request", async () => {
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
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <FindingsInboxPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(await screen.findByRole("button", { name: "Try again" })).toBeInTheDocument();
    vi.unstubAllGlobals();
  });

  it("filters by project, agent, verdict, kind, and source in the URL and the request", async () => {
    renderInbox({
      "/findings": page([makeRow()]),
      "/projects": { items: [{ id: "proj-1", name: "Greeter" }], total: 1 },
      "/agent-profiles": { items: [{ id: "agent-1", name: "Reviewer" }], total: 1 },
    });
    await screen.findByText(/Claim number/);

    await userEvent.selectOptions(screen.getByLabelText("Verdict"), "stale");
    await waitFor(() => expect(lastFetchUrl()).toContain("verdict=stale"));

    await userEvent.selectOptions(screen.getByLabelText("Kind"), "recommendation");
    await waitFor(() => expect(lastFetchUrl()).toContain("kind=recommendation"));

    await userEvent.selectOptions(screen.getByLabelText("Source"), "observed");
    await waitFor(() => expect(lastFetchUrl()).toContain("source=observed"));

    await userEvent.selectOptions(screen.getByLabelText("Project"), "proj-1");
    await waitFor(() => expect(lastFetchUrl()).toContain("project_id=proj-1"));

    await userEvent.selectOptions(screen.getByLabelText("Agent"), "agent-1");
    await waitFor(() => expect(lastFetchUrl()).toContain("agent_profile_id=agent-1"));
  });

  it("debounces the search box into the q parameter and resets the offset", async () => {
    renderInbox({ "/findings": page([makeRow()], 120) });
    await screen.findByText(/Claim number/);
    await userEvent.type(screen.getByLabelText("Search claims"), "greeter");
    await waitFor(() => expect(lastFetchUrl()).toContain("q=greeter"), { timeout: 2000 });
    expect(lastFetchUrl()).toContain("offset=0");
  });

  it("paginates at 50 and requests the next offset", async () => {
    renderInbox({ "/findings": page([makeRow()], 120) });
    await screen.findByText(/Claim number/);
    await userEvent.click(screen.getByRole("button", { name: "Next page" }));
    await waitFor(() => expect(lastFetchUrl()).toContain("offset=50"));
  });

  it("switches sort to newest via the When column header, and back", async () => {
    renderInbox({ "/findings": page([makeRow()]) });
    await screen.findByText(/Claim number/);
    await userEvent.click(screen.getByRole("button", { name: /When/ }));
    await waitFor(() => expect(lastFetchUrl()).toContain("sort=newest"));
    await userEvent.click(screen.getByRole("button", { name: /When/ }));
    await waitFor(() => expect(lastFetchUrl()).not.toContain("sort=newest"));
  });

  it("shows the empty-window state with the recording pointer when nothing is in the window", async () => {
    renderInbox({ "/findings": page([]) });
    expect(await screen.findByText("No findings in this window.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "How findings are recorded" })).toBeInTheDocument();
  });

  it("shows the no-match state with Clear filters when a filter excludes everything", async () => {
    renderInbox({ "/findings": page([]) });
    await screen.findByText("No findings in this window.");
    await userEvent.selectOptions(screen.getByLabelText("Verdict"), "stale");
    expect(await screen.findByText("No findings match these filters.")).toBeInTheDocument();
    const clear = screen.getByRole("button", { name: "Clear filters" });
    await userEvent.click(clear);
    await waitFor(() => expect(lastFetchUrl()).not.toContain("verdict="));
  });
});
