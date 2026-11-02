import { screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";

import { ToastProvider } from "@/components/ui";
import { makeDetail, makeFinding, renderWithQuery } from "@/test/fixtures";

import type { Challenge } from "./api";
import { FindingDetail } from "./FindingDetail";

// `ConfirmControl` (rendered at the bottom of the pane whenever the finding has an id, which
// every fixture below does) calls `useToast()`, which throws outside a `<ToastProvider>`.
function withToast(ui: ReactElement): ReactElement {
  return <ToastProvider>{ui}</ToastProvider>;
}

function makeChallenge(overrides: Partial<Challenge> = {}): Challenge {
  const detail = makeDetail({ id: "child-1", title: "Challenge: Improve the greeting service" });
  return {
    run: detail.run,
    trigger: "on_demand",
    state: "completed",
    findings_reviewed: ["F1"],
    omitted: 0,
    outcomes: [],
    unresolved: [],
    judgment: "model judgment",
    counts_as_proof: false,
    ...overrides,
  };
}

describe("FindingDetail Challenges section", () => {
  it("is absent when there are no challenges", async () => {
    const detail = makeDetail();
    renderWithQuery(
      withToast(<FindingDetail finding={makeFinding({ key: "F1" })} detail={detail} events={[]} />),
      {
        api: { "/runs/run-1/challenges": [] },
      },
    );
    expect(await screen.findByText("Agent declared")).toBeInTheDocument();
    expect(screen.queryByText(/^Challenges \(/)).not.toBeInTheDocument();
  });

  it("lists every challenge, newest first, with its own outcome phrase", async () => {
    const detail = makeDetail();
    const finding = makeFinding({ key: "F1" });
    const older = makeChallenge({
      run: { ...makeDetail({ id: "child-older" }).run, created_at: "2026-01-01T00:00:00Z" },
      state: "did_not_complete",
    });
    const newer = makeChallenge({
      run: { ...makeDetail({ id: "child-newer" }).run, created_at: "2026-01-02T00:00:00Z" },
      state: "completed",
      outcomes: [
        {
          target_finding_key: finding.key,
          target_finding_id: finding.id,
          outcome: "refuted",
          review_finding_id: "rf-1",
          review_finding_key: "RF1",
          text: "text",
          citations: 1,
        },
      ],
    });
    renderWithQuery(withToast(<FindingDetail finding={finding} detail={detail} events={[]} />), {
      api: { "/runs/run-1/challenges": [older, newer] },
    });
    expect(await screen.findByText("Challenges (2)")).toBeInTheDocument();
    const rows = screen.getAllByRole("listitem").filter((li) => li.textContent.includes("started"));
    expect(rows).toHaveLength(2);
    expect(rows[0]?.textContent).toContain("refuted for this finding (model judgment)");
    expect(rows[1]?.textContent).toContain("not reviewed (finding omitted)");
  });
});
