import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { BadgeStatus } from "./model";
import { VerdictBadge } from "./VerdictBadge";

/** Every status the badge can show; `satisfies` fails type-checking when a status is added
 * to `BadgeStatus` without being listed here. */
const EVERY_STATUS = Object.keys({
  quote_matches: true,
  stale: true,
  quote_not_found: true,
  never_read: true,
  read_not_observed: true,
  not_checkable: true,
  declared_only: true,
  checking: true,
  not_checked_yet: true,
  checked_when_finished: true,
} satisfies Record<BadgeStatus, true>) as BadgeStatus[];

describe("VerdictBadge", () => {
  it("says exactly 'Quote matches' for a matching quote, never a bare past-tense claim", () => {
    const { container } = render(<VerdictBadge verdict="quote_matches" />);
    expect(container).toHaveTextContent(/^Quote matches$/);
  });

  it.each(EVERY_STATUS)("renders %s as an icon plus a phrase, never a bare icon", (status) => {
    const { container } = render(<VerdictBadge verdict={status} />);
    expect(container.querySelector("svg")).not.toBeNull();
    expect(container.textContent.trim()).not.toBe("");
  });

  it("names the pending states in words", () => {
    render(
      <>
        <VerdictBadge verdict="checked_when_finished" />
        <VerdictBadge verdict="not_checked_yet" />
      </>,
    );
    expect(screen.getByText("Checked when the run finishes")).toBeInTheDocument();
    expect(screen.getByText("Not checked yet")).toBeInTheDocument();
  });

  it("appends a short label from the closed reason map, never the raw backend sentence", () => {
    const rawReason =
      "<outside>/passwd is outside the run's project boundary; AgentTape does not check citations outside the tracked repository.";
    render(
      <VerdictBadge verdict="not_checkable" reasonCode="outside_boundary" reasonText={rawReason} />,
    );
    expect(screen.getByText("Cannot check: outside project")).toBeInTheDocument();
    expect(screen.queryByText(/does not check citations outside/)).not.toBeInTheDocument();
  });

  it("carries the full reason sentence as the title, never inline, so it can never clip", () => {
    const rawReason = "raw backend text, not shown inline";
    render(
      <VerdictBadge verdict="not_checkable" reasonCode="outside_boundary" reasonText={rawReason} />,
    );
    expect(
      screen.getByTitle(
        "The cited path is outside the run's repository boundary, so no read was recorded.",
      ),
    ).toBeInTheDocument();
  });

  it("falls back to the raw reason text as the title when the code isn't in the closed map", () => {
    render(
      <VerdictBadge
        verdict="not_checkable"
        reasonCode="brand_new_reason"
        reasonText="Fresh reason from the backend."
      />,
    );
    expect(screen.getByTitle("Fresh reason from the backend.")).toBeInTheDocument();
    // No short label exists for an unmapped code, so no suffix is appended: the badge can
    // never grow unbounded even for a reason code this closed map does not yet know about.
    expect(screen.getByText("Cannot check")).toBeInTheDocument();
  });

  it("never treats an inherited object key as a known reason code", () => {
    render(
      <VerdictBadge verdict="not_checkable" reasonCode="toString" reasonText="Backend text." />,
    );
    expect(screen.getByText("Cannot check")).toBeInTheDocument();
    expect(screen.getByTitle("Backend text.")).toBeInTheDocument();
  });

  it("drops the reason suffix when compact", () => {
    render(
      <VerdictBadge
        verdict="not_checkable"
        reasonCode="outside_boundary"
        reasonText="Some reason text"
        compact
      />,
    );
    expect(screen.queryByText(/outside project/)).not.toBeInTheDocument();
  });

  it("titles a seeded verdict as a demo stand-in", () => {
    render(<VerdictBadge verdict="read_not_observed" method="seeded" />);
    expect(screen.getByTitle("Seeded demo stand-in")).toBeInTheDocument();
  });

  it("carries no title for a verdict decided by the real engine", () => {
    const { container } = render(<VerdictBadge verdict="quote_matches" method="lexical_v1" />);
    expect(container.querySelector("[title]")).not.toBeInTheDocument();
  });
});
