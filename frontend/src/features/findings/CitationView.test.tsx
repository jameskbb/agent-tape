import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";

import { ToastProvider } from "@/components/ui";
import { makeCitation, makeDetail, makeEvent, makeVerdict, renderWithQuery } from "@/test/fixtures";

import type { CitationContent, Reproduction } from "./api";
import { CitationView } from "./CitationView";

function withToast(ui: ReactElement): ReactElement {
  return <ToastProvider>{ui}</ToastProvider>;
}

let seq = 0;

function makeContent(overrides: Partial<CitationContent> = {}): CitationContent {
  return {
    path: "src/greeter.py",
    lines: [
      [1, "one"],
      [2, "two"],
      [3, "three"],
      [4, "def greet(name):"],
      [5, "    return name"],
      [6, "six"],
    ],
    line_first: 1,
    line_last: 20,
    window: [1, 6],
    cited: [4, 5],
    truncated: false,
    redacted: false,
    clipped: false,
    blob_ref: "88d083f5c3ab00000000",
    read_result_event_id: null,
    resolved_by: "verdict_read",
    unavailable_reason: null,
    ...overrides,
  };
}

function makeReproduction(overrides: Partial<Reproduction> = {}): Reproduction {
  seq += 1;
  return {
    id: `r-${seq}`,
    trigger: "automatic",
    mode: "search",
    status: "done",
    result: "reproduced",
    reason: "Same exit code and the same 1 output line at the pinned commit.",
    pinned_commit: "abc1234def0000000000",
    exit_code: 0,
    recorded_exit_code: 0,
    created_at: "2026-01-01T00:00:00Z",
    finished_at: "2026-01-01T00:00:01Z",
    ...overrides,
  };
}

const run = makeDetail().run;

function rowForLine(container: HTMLElement, lineNo: number): HTMLElement {
  const rows = container.querySelectorAll("tr");
  for (const row of rows) {
    const cells = row.querySelectorAll("td");
    if (cells[1]?.textContent.trim() === String(lineNo)) return row;
  }
  throw new Error(`no row rendered for line ${lineNo}`);
}

/** Renders with a fetch stub that always answers `status` for every request, used to force
 * a non-404 fetch failure the default `renderWithQuery` stub cannot produce. */
function renderWithFetchStatus(ui: ReactElement, status: number) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve(
        new Response(JSON.stringify({ error: { code: "internal", message: "boom" } }), {
          status,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    ),
  );
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("CitationView", () => {
  it("marks the quote_matches rows the verdict matched, with the accent rule and aria-label", async () => {
    const citation = makeCitation({
      verdict: makeVerdict("quote_matches", {
        evidence: {
          rule: "lexical_v1",
          line_range: [4, 5],
          claimed_range: [4, 5],
          claimed_range_matches: true,
          read: { sequence: 15, blob_ref: "88d083f5c3ab00000000" },
        },
      }),
    });
    renderWithQuery(<CitationView citation={citation} run={run} events={[]} />, {
      api: { [`/citations/${citation.id}/content`]: makeContent() },
    });
    const region = await screen.findByRole("region", { name: /Citation: src\/greeter\.py/ });
    const tbody = await within(region).findByRole("rowgroup", { name: "Lines 4 to 5 cited" });
    expect(within(rowForLine(tbody, 4)).getByText("›")).toBeInTheDocument();
    expect(within(rowForLine(tbody, 1)).queryByText("›")).not.toBeInTheDocument();
  });

  it("shows the Git blob provenance label in the header for a Git-anchored read", async () => {
    const citation = makeCitation({
      verdict: makeVerdict("quote_matches", {
        evidence: {
          rule: "lexical_v1",
          line_range: [4, 5],
          claimed_range: [4, 5],
          claimed_range_matches: true,
          read: { sequence: 15, blob_ref: "git:0123456789abcdef" },
        },
      }),
    });
    renderWithQuery(<CitationView citation={citation} run={run} events={[]} />, {
      api: { [`/citations/${citation.id}/content`]: makeContent() },
    });
    const region = await screen.findByRole("region", { name: /Citation: src\/greeter\.py/ });
    const label = await within(region).findByRole("button", {
      name: "Git blob 0123456",
      description: /this is not proof the lines were committed/,
    });
    expect(label).toHaveAttribute("aria-expanded", "false");
    expect(within(region).queryByText(/not proof the lines were committed/)).not.toBeVisible();
    await userEvent.click(label);
    expect(label).toHaveAttribute("aria-expanded", "true");
    expect(within(region).getByText(/not proof the lines were committed/)).toBeVisible();
  });

  it("shows the Reported output provenance label in the header for a locally retained read", async () => {
    const citation = makeCitation({
      verdict: makeVerdict("quote_matches", {
        evidence: {
          rule: "lexical_v1",
          line_range: [4, 5],
          claimed_range: [4, 5],
          claimed_range_matches: true,
          read: { sequence: 15, blob_ref: "sha256:fedcba9876543210" },
        },
      }),
    });
    renderWithQuery(<CitationView citation={citation} run={run} events={[]} />, {
      api: { [`/citations/${citation.id}/content`]: makeContent() },
    });
    const region = await screen.findByRole("region", { name: /Citation: src\/greeter\.py/ });
    const label = await within(region).findByRole("button", {
      name: "Reported output fedcba9",
      description: /not what the repository contained/,
    });
    label.focus();
    await userEvent.keyboard("{Enter}");
    expect(within(region).getByText(/not what the repository contained/)).toBeVisible();
  });

  it("requests a wider window when Show 20 more lines above is clicked", async () => {
    const citation = makeCitation({
      verdict: makeVerdict("quote_matches", {
        evidence: { rule: "lexical_v1", line_range: [4, 5], claimed_range: [4, 5] },
      }),
    });
    renderWithQuery(<CitationView citation={citation} run={run} events={[]} />, {
      api: {
        [`/citations/${citation.id}/content`]: makeContent({ line_first: -100 }),
      },
    });
    await screen.findByRole("region", { name: /Citation:/ });
    await userEvent.click(await screen.findByRole("button", { name: "Show 20 more lines above" }));
    const fetchMock = globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes("before=23"))).toBe(true);
  });

  it("disables Start of retained read once the window reaches line_first", async () => {
    const citation = makeCitation({
      verdict: makeVerdict("quote_matches", {
        evidence: { rule: "lexical_v1", line_range: [4, 5], claimed_range: [4, 5] },
      }),
    });
    renderWithQuery(<CitationView citation={citation} run={run} events={[]} />, {
      api: { [`/citations/${citation.id}/content`]: makeContent({ line_first: 1 }) },
    });
    const button = await screen.findByRole("button", { name: "Start of retained read" });
    expect(button).toBeDisabled();
  });

  it("shows the claimed and matched ranges in the header when they differ", async () => {
    const citation = makeCitation({
      line_start: 4,
      line_end: 11,
      verdict: makeVerdict("quote_matches", {
        evidence: {
          rule: "lexical_v1",
          line_range: [6, 13],
          claimed_range: [4, 11],
          claimed_range_matches: false,
        },
      }),
    });
    renderWithQuery(<CitationView citation={citation} run={run} events={[]} />, {
      api: { [`/citations/${citation.id}/content`]: makeContent({ cited: [6, 13] }) },
    });
    const region = await screen.findByRole("region", { name: /Citation:/ });
    expect(within(region).getByText(/cited L4-L11, matched at L6-L13/)).toBeInTheDocument();
  });

  it("hatches stale cited rows and names the write and both hashes by the event's own sequence", async () => {
    const writeEvent = makeEvent({ id: "evt-write", sequence: 99, type: "git_change" });
    const citation = makeCitation({
      verdict: makeVerdict("stale", {
        evidence: {
          rule: "lexical_v1",
          line_range: [4, 5],
          claimed_range: [4, 5],
          claimed_range_matches: true,
          change: {
            evidence_id: "chg-1",
            source_event_id: "evt-write",
            operation: "modify",
            ts: "2026-01-01T12:00:05.200Z",
            sequence: 24,
            source: "watcher",
          },
          later_writes: 1,
          hash_at_read: "88d083f5c3ab00000000",
          hash_at_read_matches_recorded: true,
          hash_after: "3f1c9a2b7e0000000000",
        },
      }),
    });
    renderWithQuery(<CitationView citation={citation} run={run} events={[writeEvent]} />, {
      api: { [`/citations/${citation.id}/content`]: makeContent() },
    });
    const region = await screen.findByRole("region", { name: /Citation:/ });
    expect(region.textContent).toContain("Changed at +00:05.2 by modify #99.");
    expect(region.textContent).not.toContain("#24");
    expect(region.textContent).toContain("Hash at read 88d083f5c3 became 3f1c9a2b7e.");
    // The stale rows stay marked as cited (the warning hatch over them is asserted end to
    // end: findings.spec.ts "F2's detail pane shows its hatched stale citation").
    const tbody = await within(region).findByRole("rowgroup", { name: "Lines 4 to 5 cited" });
    expect(within(rowForLine(tbody, 4)).getByText("›")).toBeInTheDocument();
  });

  it("shows Unavailable for a stale change with no recorded hash_after", async () => {
    const citation = makeCitation({
      verdict: makeVerdict("stale", {
        evidence: {
          rule: "lexical_v1",
          line_range: [4, 5],
          claimed_range: [4, 5],
          change: { operation: "delete", ts: "2026-01-01T12:00:05.200Z", sequence: 24 },
          hash_at_read: "88d083f5c3ab00000000",
          hash_at_read_matches_recorded: true,
          hash_after: null,
        },
      }),
    });
    renderWithQuery(<CitationView citation={citation} run={run} events={[]} />, {
      api: { [`/citations/${citation.id}/content`]: makeContent() },
    });
    const region = await screen.findByRole("region", { name: /Citation:/ });
    expect(region.textContent).toContain("Hash at read 88d083f5c3 became Unavailable.");
  });

  it("uses the unconfirmed-hash sentence when hash_at_read_matches_recorded is false", async () => {
    const citation = makeCitation({
      verdict: makeVerdict("stale", {
        evidence: {
          rule: "lexical_v1",
          line_range: [4, 5],
          claimed_range: [4, 5],
          change: { operation: "modify", ts: "2026-01-01T12:00:05.200Z", sequence: 24 },
          hash_at_read: "88d083f5c3ab00000000",
          hash_at_read_matches_recorded: false,
          hash_after: "3f1c9a2b7e0000000000",
        },
      }),
    });
    renderWithQuery(<CitationView citation={citation} run={run} events={[]} />, {
      api: { [`/citations/${citation.id}/content`]: makeContent() },
    });
    const region = await screen.findByRole("region", { name: /Citation:/ });
    expect(region.textContent).toContain(
      "Hash of the retained read 88d083f5c3; File Evidence recorded no matching hash for this file at read time.",
    );
    expect(region.textContent).not.toContain("became");
  });

  it("shows both the recorded lines and the agent's quote, open, for quote_not_found", async () => {
    const citation = makeCitation({
      line_start: 10,
      line_end: 11,
      quote: "Strip whitespace from names in greet().",
      verdict: makeVerdict("quote_not_found", {
        evidence: { rule: "lexical_v1", line_range: null, claimed_range: [10, 11] },
      }),
    });
    renderWithQuery(<CitationView citation={citation} run={run} events={[]} />, {
      api: {
        [`/citations/${citation.id}/content`]: makeContent({
          window: [8, 14],
          cited: [10, 11],
          lines: [
            [8, "a"],
            [9, "b"],
            [10, "- Initial greeting service."],
            [11, "- Whitespace handling."],
            [12, "c"],
            [13, "d"],
            [14, "e"],
          ],
        }),
      },
    });
    const region = await screen.findByRole("region", { name: /Citation:/ });
    expect(await within(region).findByText("Recorded lines L8-L14")).toBeInTheDocument();
    // The claimed rows are not marked as cited (their danger hatch is asserted end to end:
    // findings.spec.ts "F4, F5, and F7 show their failure states").
    expect(within(rowForLine(region, 10)).queryByText("›")).not.toBeInTheDocument();
    expect(screen.getByText("Quoted by the agent")).toBeInTheDocument();
    expect(screen.getByText("Agent-supplied text, unchecked")).toBeInTheDocument();
    expect(screen.getByText("Strip whitespace from names in greet().")).toBeInTheDocument();
  });

  it("shows only the agent's quote and the no-record sentence for never_read, with no content fetch", async () => {
    const citation = makeCitation({
      path: "tests/test_greeter.py",
      verdict: makeVerdict("never_read", {
        evidence: {
          rule: "lexical_v1",
          window: { complete: true, reads: "observed", offline_windows: [], statement: "" },
        },
      }),
    });
    renderWithQuery(<CitationView citation={citation} run={run} events={[]} />, { api: {} });
    const region = await screen.findByRole("region", { name: /Citation:/ });
    expect(region.textContent).toContain(
      "This run has no record of reading tests/test_greeter.py.",
    );
    expect(screen.getByText("Quoted by the agent")).toBeInTheDocument();
    const fetchMock = globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/content"))).toBe(false);
  });

  it("wraps a long unbroken path in the never_read sentence instead of letting it overflow the card", async () => {
    const longPath = `${"nested/".repeat(30)}long-filename-for-visual-truncation-check.py`;
    const citation = makeCitation({
      path: longPath,
      verdict: makeVerdict("never_read", {
        evidence: {
          rule: "lexical_v1",
          window: { complete: true, reads: "observed", offline_windows: [], statement: "" },
        },
      }),
    });
    renderWithQuery(<CitationView citation={citation} run={run} events={[]} />, { api: {} });
    const region = await screen.findByRole("region", { name: /Citation:/ });
    expect(region.textContent).toContain(`This run has no record of reading ${longPath}.`);
    // `overflow-wrap: anywhere` keeps the sentence wrapping at word boundaries (a short path
    // moves to the next line whole) while still breaking a path too long for any line, and
    // unlike `break-words` it shrinks the min-content size so no grid ancestor grows to fit.
    const sentence = [...region.querySelectorAll("p")].find((el) =>
      el.textContent.includes(longPath),
    );
    expect(sentence?.className).toContain("[overflow-wrap:anywhere]");
    expect(sentence?.querySelector(".break-all")).toBeNull();
  });

  it("uses the evidence window's own statement for read_not_observed, and never says never", async () => {
    const citation = makeCitation({
      path: "src/formatting.py",
      verdict: makeVerdict("read_not_observed", {
        evidence: {
          rule: "lexical_v1",
          window: {
            complete: false,
            reads: "unavailable",
            offline_windows: [],
            statement: "read activity is not captured for this adapter",
          },
        },
      }),
    });
    renderWithQuery(<CitationView citation={citation} run={run} events={[]} />, { api: {} });
    const region = await screen.findByRole("region", { name: /Citation:/ });
    expect(region.textContent).toContain(
      "read activity is not captured for this adapter. AgentTape cannot tell whether src/formatting.py was read during that gap.",
    );
    expect(region.textContent.toLowerCase()).not.toContain("never");
  });

  it("falls back to the verdict's own reason for a seeded read_not_observed stand-in with no window", async () => {
    const citation = makeCitation({
      path: "src/formatting.py",
      verdict: makeVerdict("read_not_observed", {
        method: "seeded",
        reason:
          "Seeded demo stand-in: the verifier found no read of this file in a complete window.",
        evidence: {
          note: "Seeded demo data: no check ran.",
          stand_in_for: "read_not_observed",
          engine_verdict: "never_read",
          engine_verdict_id: "v-old",
        },
      }),
    });
    renderWithQuery(<CitationView citation={citation} run={run} events={[]} />, { api: {} });
    const region = await screen.findByRole("region", { name: /Citation:/ });
    expect(region.textContent).toContain(
      "Seeded demo stand-in: the verifier found no read of this file in a complete window. AgentTape cannot tell whether src/formatting.py was read during that gap.",
    );
    expect(screen.getByTitle("Seeded demo stand-in")).toBeInTheDocument();
  });

  it("shows the not_checkable reason sentence and the read hash when present", async () => {
    const citation = makeCitation({
      verdict: makeVerdict("not_checkable", {
        evidence: {
          rule: "lexical_v1",
          not_checkable_reason: "redacted_at_cited_span",
          read: {
            evidence_id: "e1",
            result_event_id: "evt-1",
            sequence: 5,
            blob_ref: "deadbeef00112233",
          },
        },
      }),
    });
    renderWithQuery(<CitationView citation={citation} run={run} events={[]} />, { api: {} });
    expect(
      await screen.findByText(
        "Text that looks like a secret was redacted inside the cited lines, so the quote cannot be compared.",
      ),
    ).toBeInTheDocument();
    expect(screen.getAllByText("deadbeef00").length).toBeGreaterThan(0);
  });

  it("gives a not_checkable Git blob label its caveat as an accessible description", async () => {
    const citation = makeCitation({
      verdict: makeVerdict("not_checkable", {
        evidence: {
          rule: "lexical_v1",
          not_checkable_reason: "redacted_at_cited_span",
          read: { sequence: 5, blob_ref: "git:0123456789abcdef" },
        },
      }),
    });
    renderWithQuery(<CitationView citation={citation} run={run} events={[]} />, { api: {} });
    const labels = await screen.findAllByRole("button", {
      name: "Git blob 0123456",
      description: /this is not proof the lines were committed/,
    });
    const [header, body] = labels;
    expect(labels).toHaveLength(2);
    if (!header || !body) throw new Error("expected a header label and a body label");
    await userEvent.click(body);
    expect(body).toHaveAttribute("aria-expanded", "true");
    expect(header).toHaveAttribute("aria-expanded", "false");
  });

  it("shows the not_checkable reason sentence with no hash line when the evidence names no read", async () => {
    const citation = makeCitation({
      verdict: makeVerdict("not_checkable", {
        evidence: { rule: "lexical_v1", not_checkable_reason: "size_cap_exceeded" },
      }),
    });
    renderWithQuery(<CitationView citation={citation} run={run} events={[]} />, { api: {} });
    expect(
      await screen.findByText(
        "The cited text falls past the size cap on what AgentTape recorded: 1 MiB of a read, 16 KiB of an event.",
      ),
    ).toBeInTheDocument();
  });

  it("shows the unavailable sentence when the content endpoint reports unavailable_reason", async () => {
    const citation = makeCitation({
      verdict: makeVerdict("quote_matches", {
        evidence: { rule: "lexical_v1", line_range: [4, 5], claimed_range: [4, 5] },
      }),
    });
    renderWithQuery(<CitationView citation={citation} run={run} events={[]} />, {
      api: {
        [`/citations/${citation.id}/content`]: makeContent({
          lines: [],
          line_first: null,
          line_last: null,
          window: null,
          cited: null,
          unavailable_reason: "blob_unavailable",
        }),
      },
    });
    expect(
      await screen.findByText(
        "The retained content is no longer available; the verdict above was decided when it was.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("Quote matches")).toBeInTheDocument();
  });

  it("shows an error state with a retry action when the content fetch fails", async () => {
    const citation = makeCitation({
      verdict: makeVerdict("quote_matches", {
        evidence: { rule: "lexical_v1", line_range: [4, 5], claimed_range: [4, 5] },
      }),
    });
    renderWithFetchStatus(<CitationView citation={citation} run={run} events={[]} />, 500);
    expect(await screen.findByText("Could not load the retained lines.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("shows the retained-read footer notes when the content is truncated or redacted", async () => {
    const citation = makeCitation({
      verdict: makeVerdict("quote_matches", {
        evidence: { rule: "lexical_v1", line_range: [4, 5], claimed_range: [4, 5] },
      }),
    });
    renderWithQuery(<CitationView citation={citation} run={run} events={[]} />, {
      api: {
        [`/citations/${citation.id}/content`]: makeContent({
          truncated: true,
          redacted: true,
        }),
      },
    });
    expect(await screen.findByText(/Retained read cut short at a size cap/)).toBeInTheDocument();
    expect(screen.getByText(/Secrets redacted in this read/)).toBeInTheDocument();
  });

  it("says a no-range citation's recorded lines stop at the line limit", async () => {
    const citation = makeCitation({
      line_start: null,
      line_end: null,
      quote: "not in the file",
      verdict: makeVerdict("quote_not_found", {
        evidence: { rule: "lexical_v1", claimed_range: null },
      }),
    });
    const lines: [number, string][] = Array.from({ length: 800 }, (_, i) => [i + 1, `l${i + 1}`]);
    renderWithQuery(<CitationView citation={citation} run={run} events={[]} />, {
      api: {
        [`/citations/${citation.id}/content`]: makeContent({
          lines,
          line_first: 1,
          line_last: 1000,
          window: [1, 800],
          cited: null,
          clipped: true,
        }),
      },
    });
    expect(
      await screen.findByText(/No lines were cited, so this shows the first 800 lines/),
    ).toBeInTheDocument();
  });

  it("disables the more-lines controls once the view holds the line limit", async () => {
    const citation = makeCitation({
      verdict: makeVerdict("quote_matches", {
        evidence: { rule: "lexical_v1", line_range: [300, 301], claimed_range: [300, 301] },
      }),
    });
    const lines: [number, string][] = Array.from({ length: 800 }, (_, i) => [i + 1, `l${i + 1}`]);
    renderWithQuery(<CitationView citation={citation} run={run} events={[]} />, {
      api: {
        [`/citations/${citation.id}/content`]: makeContent({
          lines: lines.map(([n, t]) => [n + 100, t]),
          line_first: 1,
          line_last: 2000,
          window: [101, 900],
          cited: [300, 301],
        }),
      },
    });
    expect(
      await screen.findByRole("button", { name: "No more lines above (line limit)" }),
    ).toBeDisabled();
    expect(screen.getByRole("button", { name: "No more lines below (line limit)" })).toBeDisabled();
  });

  it("adds the truncated and redacted suffix to the agent block's sublabel", async () => {
    const citation = makeCitation({
      details: { quote_truncated: true, quote_redacted: true },
      verdict: makeVerdict("never_read", {
        evidence: {
          rule: "lexical_v1",
          window: { complete: true, reads: "observed", offline_windows: [], statement: "" },
        },
      }),
    });
    renderWithQuery(<CitationView citation={citation} run={run} events={[]} />, { api: {} });
    expect(
      await screen.findByText("Agent-supplied text, unchecked (truncated) (redacted)"),
    ).toBeInTheDocument();
  });

  it("titles a seeded verdict's badge Seeded demo stand-in", async () => {
    const citation = makeCitation({
      verdict: makeVerdict("read_not_observed", {
        method: "seeded",
        evidence: { note: "Seeded demo data: no check ran." },
      }),
    });
    renderWithQuery(<CitationView citation={citation} run={run} events={[]} />, { api: {} });
    expect(await screen.findByTitle("Seeded demo stand-in")).toBeInTheDocument();
  });

  it("keeps the verdict evidence collapsed behind Show verdict evidence, with the check count", async () => {
    const citation = makeCitation({
      checks: 3,
      verdict: makeVerdict("never_read", {
        rerun_of_id: "v-old",
        evidence: {
          rule: "lexical_v1",
          window: { complete: true, reads: "observed", offline_windows: [], statement: "" },
          marker_field: "value-from-evidence",
        },
      }),
    });
    renderWithQuery(<CitationView citation={citation} run={run} events={[]} />, { api: {} });
    const details = await screen.findByText("Show verdict evidence");
    const detailsEl = details.closest("details");
    expect(detailsEl).not.toBeNull();
    expect(detailsEl).not.toHaveAttribute("open");
    expect(screen.getByText(/value-from-evidence/)).toBeInTheDocument();
    // The check count only: the superseded verdict's id is internal and never shown.
    expect(screen.getByText("3 checks")).toBeInTheDocument();
    expect(screen.queryByText(/v-old/)).not.toBeInTheDocument();
  });
});

describe("CitationView event citations", () => {
  it("renders a command event's header from argv and marks the cited lines", async () => {
    const event = makeEvent({
      type: "command",
      sequence: 31,
      payload: {
        argv: ["git", "grep", "-n", "-F", "return name", "--", "src/greeter.py"],
        output: "src/greeter.py:1:def greet(name):\nsrc/greeter.py:2:    return name",
      },
    });
    const citation = makeCitation({
      kind: "event",
      path: null,
      line_start: null,
      line_end: null,
      quote: null,
      event_ref: "cmd_demo_search",
      event_id: event.id,
      verdict: makeVerdict("quote_matches", {
        evidence: {
          rule: "lexical_v1",
          line_range: [2, 2],
          event: {
            event_id: event.id,
            type: "command",
            sequence: event.sequence,
            content_key: "output",
          },
        },
      }),
    });
    renderWithQuery(withToast(<CitationView citation={citation} run={run} events={[event]} />), {
      api: {},
    });
    const region = await screen.findByRole("region", { name: /Citation:/ });
    const link = within(region).getByRole("link", { name: "#31" });
    expect(link).toHaveAttribute("href", expect.stringContaining("tab=timeline&event=31"));
    expect(region.textContent).toContain("Command");
    expect(region.textContent).toContain("git grep -n -F return name -- src/greeter.py");
    expect(within(rowForLine(region, 2)).getByText("›")).toBeInTheDocument();
    expect(within(rowForLine(region, 1)).queryByText("›")).not.toBeInTheDocument();
  });

  it("says a read's event citation was checked against the retained output, not the event text", async () => {
    const event = makeEvent({
      type: "tool_result",
      sequence: 9,
      payload: { output: "     1\tfirst\n     2\tsecond", output_truncated: true },
    });
    const citation = makeCitation({
      kind: "event",
      path: null,
      line_start: null,
      line_end: null,
      quote: "line 590",
      event_ref: "toolu_R1",
      event_id: event.id,
      verdict: makeVerdict("quote_matches", {
        evidence: {
          rule: "lexical_v1",
          line_range: [590, 590],
          event: {
            event_id: event.id,
            type: "tool_result",
            sequence: 9,
            content_key: "output",
            retained: true,
          },
        },
      }),
    });
    renderWithQuery(withToast(<CitationView citation={citation} run={run} events={[event]} />), {
      api: {},
    });
    const region = await screen.findByRole("region", { name: /Citation:/ });
    expect(region.textContent).toContain(
      "Checked against the full retained read (up to 1 MiB). Only the first 16 KiB of this event is shown here.",
    );
  });

  it("shows the recorded rows unmarked plus the agent's quote for quote_not_found", async () => {
    const event = makeEvent({
      type: "command",
      sequence: 7,
      payload: { argv: ["python", "-c", "tests"], output: "2 checks passed" },
    });
    const citation = makeCitation({
      kind: "event",
      path: null,
      quote: "3 checks passed",
      event_ref: "cmd_demo_tests",
      event_id: event.id,
      verdict: makeVerdict("quote_not_found", {
        evidence: {
          rule: "lexical_v1",
          line_range: null,
          event: { event_id: event.id, type: "command", sequence: 7, content_key: "output" },
        },
      }),
    });
    renderWithQuery(withToast(<CitationView citation={citation} run={run} events={[event]} />), {
      api: {},
    });
    const region = await screen.findByRole("region", { name: /Citation:/ });
    expect(within(rowForLine(region, 1)).queryByText("›")).not.toBeInTheDocument();
    expect(screen.getByText("Quoted by the agent")).toBeInTheDocument();
    expect(screen.getByText("3 checks passed")).toBeInTheDocument();
  });

  it("renders the event_ref and the window-rule body for an unresolved event reference", async () => {
    const citation = makeCitation({
      kind: "event",
      path: null,
      line_start: null,
      line_end: null,
      event_ref: "cmd_missing",
      event_id: null,
      verdict: makeVerdict("never_read", {
        evidence: { rule: "lexical_v1", window: { complete: true, reads: "observed" } },
      }),
    });
    renderWithQuery(withToast(<CitationView citation={citation} run={run} events={[]} />), {
      api: {},
    });
    const region = await screen.findByRole("region", { name: /Citation:/ });
    expect(within(region).getAllByText("cmd_missing").length).toBeGreaterThan(0);
    expect(await within(region).findByText("Quoted by the agent")).toBeInTheDocument();
    expect(region.textContent).toContain("This run has no record of reading cmd_missing.");
  });

  it.each([
    [
      makeReproduction({ status: "queued", result: null, reason: null, pinned_commit: null }),
      "Re-run queued",
    ],
    [
      makeReproduction({ status: "running", result: null, reason: null, pinned_commit: null }),
      "Re-run running",
    ],
    [
      makeReproduction({
        result: "not_reproduced",
        reason: "2 output lines now, 1 recorded; 1 differ.",
      }),
      "Re-run not reproduced at abc1234: 2 output lines now, 1 recorded; 1 differ.",
    ],
    [
      makeReproduction({
        result: "could_not_run",
        reason: "An argument names a path outside the repository.",
        pinned_commit: null,
      }),
      "Re-run could not run: An argument names a path outside the repository.",
    ],
  ])("shows the reproduction line %#", async (reproduction, expectedText) => {
    const event = makeEvent({ type: "command", sequence: 12 });
    const citation = makeCitation({
      kind: "event",
      path: null,
      event_ref: "cmd_demo_search",
      event_id: event.id,
      reproduction,
      verdict: makeVerdict("quote_matches", {
        evidence: {
          rule: "lexical_v1",
          line_range: [1, 1],
          event: { event_id: event.id, type: "command", sequence: 12, content_key: "output" },
        },
      }),
    });
    renderWithQuery(withToast(<CitationView citation={citation} run={run} events={[event]} />), {
      api: {},
    });
    expect(await screen.findByText(expectedText)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Re-run command" })).not.toBeInTheDocument();
  });

  it("shows a Re-run command button only when there is no reproduction yet and the run is terminal", async () => {
    const event = makeEvent({ type: "command", sequence: 12 });
    const citation = makeCitation({
      kind: "event",
      path: null,
      event_ref: "cmd_demo_search",
      event_id: event.id,
      reproduction: null,
      verdict: makeVerdict("quote_matches", {
        evidence: {
          rule: "lexical_v1",
          line_range: [1, 1],
          event: { event_id: event.id, type: "command", sequence: 12, content_key: "output" },
        },
      }),
    });
    renderWithQuery(withToast(<CitationView citation={citation} run={run} events={[event]} />), {
      api: {},
    });
    const { container: terminalContainer } = renderWithQuery(
      withToast(<CitationView citation={citation} run={run} events={[event]} />),
      { api: {} },
    );
    expect(
      await within(terminalContainer).findByRole("button", { name: "Re-run command" }),
    ).toBeInTheDocument();

    const { container: liveContainer } = renderWithQuery(
      withToast(
        <CitationView citation={citation} run={{ ...run, status: "running" }} events={[event]} />,
      ),
      { api: {} },
    );
    expect(
      within(liveContainer).queryByRole("button", { name: "Re-run command" }),
    ).not.toBeInTheDocument();
  });

  it("posts a re-run and toasts Re-run queued on success", async () => {
    const event = makeEvent({ type: "command", sequence: 12 });
    const citation = makeCitation({
      kind: "event",
      path: null,
      event_ref: "cmd_demo_search",
      event_id: event.id,
      reproduction: null,
      verdict: makeVerdict("quote_matches", {
        evidence: {
          rule: "lexical_v1",
          line_range: [1, 1],
          event: { event_id: event.id, type: "command", sequence: 12, content_key: "output" },
        },
      }),
    });
    renderWithQuery(withToast(<CitationView citation={citation} run={run} events={[event]} />), {
      api: { [`/citations/${citation.id}/reproductions`]: makeReproduction({ status: "queued" }) },
    });
    await userEvent.click(await screen.findByRole("button", { name: "Re-run command" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Re-run queued");
    const fetchMock = globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/reproductions"))).toBe(
      true,
    );
  });

  it("toasts the 409 message verbatim when a re-run is already in progress", async () => {
    const event = makeEvent({ type: "command", sequence: 12 });
    const citation = makeCitation({
      kind: "event",
      path: null,
      event_ref: "cmd_demo_search",
      event_id: event.id,
      reproduction: null,
      verdict: makeVerdict("quote_matches", {
        evidence: {
          rule: "lexical_v1",
          line_range: [1, 1],
          event: { event_id: event.id, type: "command", sequence: 12, content_key: "output" },
        },
      }),
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(
            JSON.stringify({
              error: {
                code: "conflict",
                message: "A re-run for this citation is already in progress",
              },
            }),
            { status: 409, headers: { "Content-Type": "application/json" } },
          ),
        ),
      ),
    );
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          {withToast(<CitationView citation={citation} run={run} events={[event]} />)}
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await userEvent.click(await screen.findByRole("button", { name: "Re-run command" }));
    expect(
      await screen.findByText("A re-run for this citation is already in progress"),
    ).toBeInTheDocument();
  });
});
