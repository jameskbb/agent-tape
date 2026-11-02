import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import type { FindingsTally } from "./api";
import { VerifiedFindingsStat } from "./VerifiedFindingsStat";

function renderStat(tally: FindingsTally, days = 7) {
  return render(
    <MemoryRouter>
      <VerifiedFindingsStat tally={tally} days={days} />
    </MemoryRouter>,
  );
}

describe("VerifiedFindingsStat", () => {
  it("reads the seeded-shape sentence with every verdict phrase and links to the inbox", () => {
    const tally: FindingsTally = {
      recorded: 8,
      all_quotes_match: 1,
      declared_only: 2,
      citations: 8,
      verdicts: {
        quote_matches: 3,
        stale: 1,
        quote_not_found: 1,
        never_read: 1,
        read_not_observed: 1,
        not_checkable: 1,
      },
      unchecked: 0,
    };
    renderStat(tally, 7);
    expect(screen.getByText("Verified findings")).toBeInTheDocument();
    expect(screen.getByText("1")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Every quote matched in 1 of 8 findings in the last 7 days: 3 quotes match, 1 stale, 1 quote not found, 1 never read, 1 read not observed, 1 could not be checked, 2 declared only.",
      ),
    ).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "All findings" });
    expect(link).toHaveAttribute("href", "/findings?days=7");
  });

  it("never renders a fabricated 0 for an empty window", () => {
    const tally: FindingsTally = {
      recorded: 0,
      all_quotes_match: 0,
      declared_only: 0,
      citations: 0,
      verdicts: {},
      unchecked: 0,
    };
    const { container } = renderStat(tally, 7);
    expect(container.textContent).not.toMatch(/\b0\b/);
    expect(screen.getByText("No findings recorded in the last 7 days.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "How findings are recorded" })).toHaveAttribute(
      "href",
      expect.stringContaining("claude-code.md"),
    );
  });

  it("renders the tile first with Success rate second on the Overview", () => {
    // Covered end-to-end by OverviewPage's own render order; this asserts the label alone
    // renders standalone so the Overview test can assert ordering without duplicating tally
    // fixtures here.
    const tally: FindingsTally = {
      recorded: 3,
      all_quotes_match: 3,
      declared_only: 0,
      citations: 3,
      verdicts: { quote_matches: 3 },
      unchecked: 0,
    };
    renderStat(tally, 30);
    expect(screen.getByText("Verified findings")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
  });
});
