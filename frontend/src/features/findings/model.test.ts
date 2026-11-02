import { describe, expect, it } from "vitest";

import { makeCitation, makeDetail, makeFinding, makeVerdict } from "@/test/fixtures";

import type { Challenge, Finding, Reproduction } from "./api";
import {
  citationTally,
  findingMatchesChip,
  ladder,
  pendingState,
  readProvenance,
  statementText,
  tallyFromCounts,
  tallyPhrases,
  worstVerdict,
} from "./model";

let seq = 0;

/** The seeded-demo shape from the plan: 8 findings, 8 citations (3 quote_matches, 1 of each
 * other verdict), 2 declared only. */
function eightFindingFixture(): Finding[] {
  return [
    makeFinding([
      makeCitation("quote_matches"),
      makeCitation("quote_matches"),
      makeCitation("quote_matches"),
    ]),
    makeFinding([makeCitation("stale")]),
    makeFinding([makeCitation("quote_not_found")]),
    makeFinding([makeCitation("never_read")]),
    makeFinding([makeCitation("read_not_observed")]),
    makeFinding([makeCitation("not_checkable")]),
    makeFinding([]),
    makeFinding([]),
  ];
}

describe("citationTally", () => {
  it("tallies every citation's latest verdict, plus declared-only findings", () => {
    expect(citationTally(eightFindingFixture())).toEqual({
      quote_matches: 3,
      stale: 1,
      quote_not_found: 1,
      never_read: 1,
      read_not_observed: 1,
      not_checkable: 1,
      declared_only: 2,
      unchecked: 0,
    });
  });

  it("counts a null verdict as unchecked", () => {
    expect(citationTally([makeFinding([makeCitation(null)])])).toEqual(
      expect.objectContaining({ unchecked: 1, quote_matches: 0 }),
    );
  });
});

describe("tallyFromCounts", () => {
  it("reads each verdict's count by key and counts a missing key as 0", () => {
    expect(
      tallyFromCounts({ quote_matches: 3, stale: 1, declared_only: 2, unchecked: 1, other: 9 }),
    ).toEqual({
      quote_matches: 3,
      stale: 1,
      quote_not_found: 0,
      never_read: 0,
      read_not_observed: 0,
      not_checkable: 0,
      declared_only: 2,
      unchecked: 1,
    });
  });
});

describe("tallyPhrases", () => {
  it("renders the comma-separated tally, zero counts omitted", () => {
    const tally = citationTally(eightFindingFixture());
    expect(tallyPhrases(tally)).toBe(
      "3 quotes match, 1 stale, 1 quote not found, 1 never read, 1 read not observed, 1 could not be checked, 2 declared only",
    );
  });

  it("reads a single match as '1 quote matches'", () => {
    const tally = citationTally([makeFinding([makeCitation("quote_matches")])]);
    expect(tallyPhrases(tally)).toBe("1 quote matches");
  });

  it("adds a not-checked-yet phrase for null verdicts, before declared only", () => {
    const tally = citationTally([makeFinding([makeCitation(null)]), makeFinding([])]);
    expect(tallyPhrases(tally)).toBe("1 not checked yet, 1 declared only");
  });
});

describe("worstVerdict", () => {
  it("picks the worst verdict among a finding's citations", () => {
    const finding = makeFinding([
      makeCitation("quote_matches"),
      makeCitation("stale"),
      makeCitation("quote_not_found"),
    ]);
    expect(worstVerdict(finding)).toBe("quote_not_found");
  });

  it("treats a finding with no citations as declared_only", () => {
    expect(worstVerdict(makeFinding([]))).toBe("declared_only");
  });

  it("ranks worst to best as quote_not_found, never_read, stale, not_checkable, read_not_observed, unchecked, quote_matches", () => {
    const order = [
      "quote_not_found",
      "never_read",
      "stale",
      "not_checkable",
      "read_not_observed",
      null,
      "quote_matches",
    ] as const;
    order.forEach((worse, index) => {
      for (const better of order.slice(index + 1)) {
        const finding = makeFinding([makeCitation(better), makeCitation(worse)]);
        expect(worstVerdict(finding)).toBe(worse ?? "unchecked");
      }
    });
  });
});

describe("findingMatchesChip", () => {
  it("matches when any citation's latest verdict equals the chip", () => {
    const finding = makeFinding([makeCitation("quote_matches"), makeCitation("stale")]);
    expect(findingMatchesChip(finding, "stale")).toBe(true);
    expect(findingMatchesChip(finding, "never_read")).toBe(false);
  });

  it("matches the declared_only chip only for a finding with zero citations", () => {
    expect(findingMatchesChip(makeFinding([]), "declared_only")).toBe(true);
    expect(findingMatchesChip(makeFinding([makeCitation("stale")]), "declared_only")).toBe(false);
  });
});

describe("pendingState", () => {
  const runningRun = { status: "running" as const, finished_at: null };
  const finishedRun = (finishedAt: string) => ({
    status: "succeeded" as const,
    finished_at: finishedAt,
  });

  it("returns null once a citation carries a verdict", () => {
    expect(pendingState(runningRun, makeCitation("quote_matches"), Date.now())).toBeNull();
  });

  it("is checked_when_finished while the run is live", () => {
    expect(pendingState(runningRun, makeCitation(null), Date.now())).toBe("checked_when_finished");
  });

  it("is checking within 30 s of finished_at, then not_checked_yet", () => {
    const finishedAt = "2026-01-01T00:00:00.000Z";
    const run = finishedRun(finishedAt);
    const citation = makeCitation(null);
    const justAfter = new Date(finishedAt).getTime() + 5_000;
    const wellAfter = new Date(finishedAt).getTime() + 31_000;
    expect(pendingState(run, citation, justAfter)).toBe("checking");
    expect(pendingState(run, citation, wellAfter)).toBe("not_checked_yet");
  });
});

describe("statementText", () => {
  it("gives the normal terminal statement with the tally and the caveat", () => {
    const text = statementText(eightFindingFixture(), false);
    expect(text).toContain("AgentTape checked 8 citations in 8 findings");
    expect(text).toContain("3 quotes match, 1 stale");
    expect(text).toContain(
      "Checkable, not foolproof: a matching quote shows the lines existed when they were read, not that the conclusion is right.",
    );
  });

  it("gives the live statement regardless of tally", () => {
    expect(statementText(eightFindingFixture(), true)).toBe(
      "This run is still recording. Citations are checked once it finishes.",
    );
  });

  it("gives the all-declared-only statement when no finding has a citation", () => {
    const findings = [makeFinding([]), makeFinding([])];
    expect(statementText(findings, false)).toBe(
      "This run recorded 2 findings and none of them cites lines or a recorded event, so there is nothing to check.",
    );
  });

  it("gives the checking statement when a terminal run has decided nothing yet", () => {
    const findings = [makeFinding([makeCitation(null), makeCitation(null)])];
    expect(statementText(findings, false)).toBe("Checking 2 citations against the record.");
  });
});

/* ----------------------------------------------------------------------------------- */
/* ladder()                                                                             */
/* ----------------------------------------------------------------------------------- */

function reproductionWith(overrides: Partial<Reproduction> = {}): Reproduction {
  seq += 1;
  return {
    id: `r-${seq}`,
    trigger: "automatic",
    mode: "search",
    status: "done",
    result: "reproduced",
    reason: null,
    pinned_commit: "0123456789abcdef",
    exit_code: 0,
    recorded_exit_code: 0,
    created_at: "2026-01-01T00:00:00Z",
    finished_at: "2026-01-01T00:00:01Z",
    ...overrides,
  };
}

function challengeWith(overrides: Partial<Challenge> = {}): Challenge {
  seq += 1;
  return {
    run: { ...makeDetail().run, id: `child-${seq}`, created_at: "2026-01-01T00:00:00Z" },
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

describe("ladder", () => {
  it("always returns six rungs in fixed order, slot 5 always naming model judgment", () => {
    const rungs = ladder(makeFinding([]), [], Date.now());
    expect(rungs.map((r) => r.slot)).toEqual([1, 2, 3, 4, 5, 6]);
    for (const rung of rungs) expect(rung.label.length).toBeGreaterThan(0);
    expect(rungs[4].label).toContain("model judgment");
  });

  it("gives every rung its stable UI-SPEC name, whatever its current label", () => {
    const rungs = ladder(makeFinding([]), [], Date.now());
    expect(rungs.map((r) => r.name)).toEqual([
      "Agent declared",
      "Quotes match",
      "Event observed",
      "Reproduced",
      "Challenge",
      "Confirmed",
    ]);
  });

  it("rung 1 is always achieved: Agent declared", () => {
    const [rung1] = ladder(makeFinding([]), [], Date.now());
    expect(rung1).toMatchObject({ label: "Agent declared", achieved: true });
  });

  it("rung 2 reads all quotes matching as achieved", () => {
    const finding = makeFinding([makeCitation({ verdict: makeVerdict("quote_matches") })]);
    const [, rung2] = ladder(finding, [], Date.now());
    expect(rung2).toMatchObject({ label: "Quotes match (1 of 1)", achieved: true });
  });

  it("rung 2 reads a stale citation among matches as not achieved", () => {
    const finding = makeFinding([
      makeCitation({ verdict: makeVerdict("quote_matches") }),
      makeCitation({ verdict: makeVerdict("stale") }),
    ]);
    const [, rung2] = ladder(finding, [], Date.now());
    expect(rung2).toMatchObject({ label: "Quotes match: 1 of 2", achieved: false });
  });

  it("rung 2 reads no citations as 'no citation'", () => {
    const [, rung2] = ladder(makeFinding([]), [], Date.now());
    expect(rung2).toMatchObject({ label: "Quotes match: no citation", achieved: false });
  });

  it("rung 3 reads a matched event citation as observed", () => {
    const finding = makeFinding([
      makeCitation({ kind: "event", event_ref: "e1", verdict: makeVerdict("quote_matches") }),
    ]);
    const [, , rung3] = ladder(finding, [], Date.now());
    expect(rung3).toMatchObject({ label: "Event observed", achieved: true });
  });

  it("rung 3 reads a file-only finding as no event cited", () => {
    const finding = makeFinding([makeCitation({ verdict: makeVerdict("quote_matches") })]);
    const [, , rung3] = ladder(finding, [], Date.now());
    expect(rung3).toMatchObject({ label: "No event cited", achieved: false });
  });

  it("rung 3 reads a cited-but-unmatched event as its own honest label", () => {
    const finding = makeFinding([
      makeCitation({ kind: "event", event_ref: "e1", verdict: makeVerdict("quote_not_found") }),
    ]);
    const [, , rung3] = ladder(finding, [], Date.now());
    expect(rung3).toMatchObject({ label: "Event cited, quote not matched", achieved: false });
  });

  it("rung 4 reads a reproduced citation as achieved", () => {
    const finding = makeFinding([makeCitation({ reproduction: reproductionWith() })]);
    const [, , , rung4] = ladder(finding, [], Date.now());
    expect(rung4).toMatchObject({ label: "Reproduced by AgentTape", achieved: true });
  });

  it("rung 4 reads a queued re-run as queued", () => {
    const finding = makeFinding([
      makeCitation({ reproduction: reproductionWith({ status: "queued", result: null }) }),
    ]);
    const [, , , rung4] = ladder(finding, [], Date.now());
    expect(rung4).toMatchObject({ label: "Re-run queued", achieved: false });
  });

  it("rung 4 reads a could-not-run re-run as its own label", () => {
    const finding = makeFinding([
      makeCitation({ reproduction: reproductionWith({ result: "could_not_run" }) }),
    ]);
    const [, , , rung4] = ladder(finding, [], Date.now());
    expect(rung4).toMatchObject({ label: "Could not run", achieved: false });
  });

  it("rung 4 reads no reproduction at all as not re-run", () => {
    const [, , , rung4] = ladder(makeFinding([makeCitation()]), [], Date.now());
    expect(rung4).toMatchObject({ label: "Not re-run", achieved: false });
  });

  it("rung 5 reads no challenges as not challenged, with no child link", () => {
    const [, , , , rung5] = ladder(makeFinding([]), [], Date.now());
    expect(rung5).toMatchObject({ label: "Not challenged (model judgment)", achieved: false });
    expect(rung5.href).toBeUndefined();
  });

  it("rung 5 reads a pending challenge as running, linking to the child", () => {
    const challenge = challengeWith({ state: "pending", outcomes: [] });
    const [, , , , rung5] = ladder(makeFinding([]), [challenge], Date.now());
    expect(rung5.label).toBe("Challenge running (model judgment)");
    expect(rung5.href).toBe(`/runs/${challenge.run.id}`);
  });

  it("rung 5 reads a did-not-complete challenge as its own label", () => {
    const challenge = challengeWith({ state: "did_not_complete" });
    const [, , , , rung5] = ladder(makeFinding([]), [challenge], Date.now());
    expect(rung5.label).toBe("Challenge did not complete (model judgment)");
  });

  it("rung 5 reads a completed outcome for this finding's key as refuted, tone violet", () => {
    const finding = makeFinding([]);
    const challenge = challengeWith({
      outcomes: [
        {
          target_finding_key: finding.key,
          target_finding_id: finding.id,
          outcome: "refuted",
          review_finding_id: "rf-1",
          review_finding_key: "RF1",
          text: "The change does not do what the finding claims.",
          citations: 1,
        },
      ],
    });
    const [, , , , rung5] = ladder(finding, [challenge], Date.now());
    expect(rung5).toMatchObject({
      label: "Challenge: refuted (model judgment)",
      achieved: true,
      tone: "violet",
    });
  });

  it("rung 5 reads upheld and inconclusive with the same shape as refuted", () => {
    const finding = makeFinding([]);
    for (const outcome of ["upheld", "inconclusive"] as const) {
      const challenge = challengeWith({
        outcomes: [
          {
            target_finding_key: finding.key,
            target_finding_id: finding.id,
            outcome,
            review_finding_id: "rf-1",
            review_finding_key: "RF1",
            text: "text",
            citations: 1,
          },
        ],
      });
      const [, , , , rung5] = ladder(finding, [challenge], Date.now());
      expect(rung5.label).toBe(`Challenge: ${outcome} (model judgment)`);
      expect(rung5.tone).toBe("violet");
    }
  });

  it("rung 5 reads a completed challenge that omitted this finding as not reviewed", () => {
    const challenge = challengeWith({ outcomes: [] });
    const [, , , , rung5] = ladder(makeFinding([]), [challenge], Date.now());
    expect(rung5.label).toBe("Challenge: not reviewed (model judgment)");
  });

  it("rung 5 follows the newer of two challenges by run.created_at", () => {
    const older = challengeWith({
      run: { ...makeDetail().run, id: "older", created_at: "2026-01-01T00:00:00Z" },
      state: "did_not_complete",
    });
    const newer = challengeWith({
      run: { ...makeDetail().run, id: "newer", created_at: "2026-01-02T00:00:00Z" },
      state: "pending",
    });
    const [, , , , rung5] = ladder(makeFinding([]), [older, newer], Date.now());
    expect(rung5.href).toBe("/runs/newer");
  });

  it("rung 6 reads a confirmation as achieved with the actor and relative time", () => {
    const finding = makeFinding([]);
    finding.confirmation = { actor: "James", ts: "2026-01-01T00:00:00Z", note: null };
    const now = new Date("2026-01-01T00:01:00Z").getTime();
    const [, , , , , rung6] = ladder(finding, [], now);
    expect(rung6.achieved).toBe(true);
    expect(rung6.label).toContain("Confirmed by James,");
  });

  it("rung 6 reads no confirmation as not confirmed", () => {
    const [, , , , , rung6] = ladder(makeFinding([]), [], Date.now());
    expect(rung6).toMatchObject({ label: "Not confirmed", achieved: false });
  });
});

describe("readProvenance", () => {
  it("labels a Git-anchored read as a stored Git object, not as committed content", () => {
    expect(readProvenance("git:0123456789abcdef")).toEqual({
      label: "Git blob 0123456",
      caveat:
        "Stored as a Git object in the run's repository, which AgentTape checked exists. Anything that can write to the repository can add such an object, so this is not proof the lines were committed.",
    });
  });

  it("labels a locally retained read as reported-only", () => {
    expect(readProvenance("sha256:fedcba9876543210")).toEqual({
      label: "Reported output fedcba9",
      caveat:
        "Retained from the output the agent reported, not anchored to a Git blob: it shows what was reported, not what the repository contained.",
    });
  });

  it("returns null for no blob reference", () => {
    expect(readProvenance(null)).toBeNull();
  });

  it("falls back to a plain 10-character prefix with no caveat for an unrecognized form", () => {
    expect(readProvenance("evidence_read:abcdef0123456789")).toEqual({
      label: "evidence_r",
    });
  });
});
