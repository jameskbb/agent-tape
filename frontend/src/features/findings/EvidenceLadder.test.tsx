import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import { makeCitation, makeDetail, makeFinding, makeVerdict } from "@/test/fixtures";

import type { Challenge, Finding } from "./api";
import { EvidenceLadder } from "./EvidenceLadder";

function renderLadder(finding: Finding, challenges: Challenge[] = []) {
  return render(
    <MemoryRouter>
      <EvidenceLadder finding={finding} challenges={challenges} />
    </MemoryRouter>,
  );
}

describe("EvidenceLadder", () => {
  it("renders the fixed aria-label with six rung buttons in order", () => {
    renderLadder(makeFinding());
    const list = screen.getByRole("list", { name: "Evidence for this finding" });
    const buttons = within(list).getAllByRole("button");
    expect(buttons).toHaveLength(6);
    expect(buttons.map((b) => b.textContent)).toEqual([
      "Agent declared",
      "Quotes match: no citation",
      "No event cited",
      "Not re-run",
      "Not challenged (model judgment)",
      "Not confirmed",
    ]);
  });

  it("pressing a rung shows its explanation, aria-expanded true; pressing again hides it", async () => {
    const user = userEvent.setup();
    renderLadder(makeFinding());
    const quotesButton = screen.getByRole("button", { name: /Quotes match/ });
    expect(quotesButton).toHaveAttribute("aria-expanded", "false");
    await user.click(quotesButton);
    expect(quotesButton).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("region", { name: "About Quotes match" })).toHaveTextContent(
      "Each cited quote appears in what this run read or recorded, at the matched lines.",
    );
    await user.click(quotesButton);
    expect(quotesButton).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("region", { name: "About Quotes match" })).not.toBeInTheDocument();
  });

  it("switching to a different rung shows only that rung's explanation", async () => {
    const user = userEvent.setup();
    renderLadder(makeFinding());
    await user.click(screen.getByRole("button", { name: "Agent declared" }));
    expect(screen.getByRole("region", { name: "About Agent declared" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Quotes match/ }));
    expect(screen.queryByRole("region", { name: "About Agent declared" })).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "About Quotes match" })).toBeInTheDocument();
  });

  it("Tab reaches every rung button in order", async () => {
    const user = userEvent.setup();
    renderLadder(makeFinding());
    const buttons = screen.getAllByRole("button");
    for (const button of buttons) {
      await user.tab();
      expect(document.activeElement).toBe(button);
    }
  });

  it("an achieved rung never renders a bare icon: text and icon both present", () => {
    const finding = makeFinding({
      citations: [makeCitation({ verdict: makeVerdict("quote_matches") })],
    });
    renderLadder(finding);
    const button = screen.getByRole("button", { name: /Quotes match \(1 of 1\)/ });
    expect(button.querySelector("svg")).not.toBeNull();
    expect(button.textContent.trim().length).toBeGreaterThan(0);
  });

  it("an unachieved rung says so in words, never by colour alone", () => {
    renderLadder(makeFinding());
    for (const name of [
      "Quotes match: no citation",
      "No event cited",
      "Not re-run",
      "Not challenged (model judgment)",
      "Not confirmed",
    ]) {
      expect(screen.getByRole("button", { name })).toBeInTheDocument();
    }
  });

  it("renders every unachieved rung outlined, never in a status tone", () => {
    renderLadder(makeFinding());
    const list = screen.getByRole("list", { name: "Evidence for this finding" });
    for (const button of within(list).getAllByRole("button")) {
      if (button.textContent === "Agent declared") continue;
      expect(button).toHaveAttribute("data-tone", "outline");
    }
  });

  it("an achieved rung carries its own tone instead of the outline", () => {
    const finding = makeFinding({
      citations: [makeCitation({ verdict: makeVerdict("quote_matches") })],
    });
    renderLadder(finding);
    expect(screen.getByRole("button", { name: /Quotes match \(1 of 1\)/ })).toHaveAttribute(
      "data-tone",
      "success",
    );
  });

  it("the Challenge rung links to the child run whenever a challenge exists", () => {
    const finding = makeFinding();
    const challenge: Challenge = {
      run: { ...makeDetail().run, id: "child-1", created_at: "2026-01-01T00:00:00Z" },
      trigger: "on_demand",
      state: "pending",
      findings_reviewed: [finding.key],
      omitted: 0,
      outcomes: [],
      unresolved: [],
      judgment: "model judgment",
      counts_as_proof: false,
    };
    renderLadder(finding, [challenge]);
    const link = screen.getByRole("link", { name: "Open the challenge run" });
    expect(link).toHaveAttribute("href", "/runs/child-1");
  });

  it("renders no link on the Challenge rung when there is no challenge", () => {
    renderLadder(makeFinding());
    expect(screen.queryByRole("link", { name: "Open the challenge run" })).not.toBeInTheDocument();
  });
});
