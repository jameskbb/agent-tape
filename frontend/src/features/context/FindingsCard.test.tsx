import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { makeCitation, makeFinding, renderWithQuery } from "@/test/fixtures";

import type { LinkedSource } from "@/features/findings/api";

import { FindingsCard } from "./FindingsCard";

function makeSource(key: string, name: string): LinkedSource {
  return {
    source_id: `src-${key}`,
    key,
    name,
    type: "file",
    live: true,
    authority: "repository",
    origin: "repository",
  };
}

const noOpen = () => {};

describe("FindingsCard", () => {
  it("shows 'Findings (n)' and a row per finding with kind, verdict, claim, and a link to the Findings tab", async () => {
    const findings = [
      makeFinding([makeCitation("quote_matches")], { key: "F1" }),
      makeFinding([makeCitation("stale")], { key: "F2" }),
    ];
    renderWithQuery(<FindingsCard runId="run-1" live={false} onOpen={noOpen} />, {
      api: { "/runs/run-1/context/findings": findings },
    });
    expect(await screen.findByText("Findings (2)")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Kind, verdict, and the sources each finding links to. Open a finding to see its cited lines.",
      ),
    ).toBeInTheDocument();
    const rows = screen.getAllByTestId("finding");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("Claim");
    expect(rows[0]).toHaveTextContent("greet() trims surrounding whitespace");
    const links = screen.getAllByRole("link", { name: "Open on Findings tab" });
    expect(links[0]).toHaveAttribute("href", "/runs/run-1?tab=findings&finding=F1");
    expect(links[1]).toHaveAttribute("href", "/runs/run-1?tab=findings&finding=F2");
  });

  it("renders each finding's own Supports, Conflicts, and Missing chips, and a chip opens its source", async () => {
    const onOpen = vi.fn();
    const finding = {
      ...makeFinding([makeCitation("quote_matches")], { key: "F1" }),
      supports: [makeSource("s1", "README.md")],
      conflicts: [makeSource("s2", "docs/old.md")],
      missing: [],
    };
    renderWithQuery(<FindingsCard runId="run-1" live={false} onOpen={onOpen} />, {
      api: { "/runs/run-1/context/findings": [finding] },
    });
    const row = await screen.findByTestId("finding");
    expect(row).toHaveTextContent("Supports");
    expect(row).toHaveTextContent("Conflicts");
    expect(row).not.toHaveTextContent("Missing");
    await userEvent.click(screen.getByRole("button", { name: /README\.md/ }));
    expect(onOpen).toHaveBeenCalledWith("src-s1");
    expect(screen.getByRole("button", { name: /docs\/old\.md/ })).toBeInTheDocument();
  });

  it("shows the no-findings sentence when there are none", async () => {
    renderWithQuery(<FindingsCard runId="run-1" live={false} onOpen={noOpen} />, {
      api: { "/runs/run-1/context/findings": [] },
    });
    expect(
      await screen.findByText("The agent reported no structured findings."),
    ).toBeInTheDocument();
  });

  it("never renders CitationView or cited lines", async () => {
    // An undecided citation: CitationView would render its labelled region and the agent's
    // quote ("hello") for it, with no content fetch, so both absences below are observable.
    const findings = [makeFinding([makeCitation(null)], { key: "F1" })];
    renderWithQuery(<FindingsCard runId="run-1" live={false} onOpen={noOpen} />, {
      api: { "/runs/run-1/context/findings": findings },
    });
    await screen.findByTestId("finding");
    expect(screen.queryByRole("region", { name: /^Citation:/ })).not.toBeInTheDocument();
    expect(screen.queryByText("hello")).not.toBeInTheDocument();
  });
});
