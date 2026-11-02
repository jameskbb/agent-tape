/**
 * The one set of definitions every findings surface derives from: the tally behind the
 * statement card and the tally chips, the worst-verdict rank behind the list row and run
 * header badges, and the pending-verdict state shown while a run finishes verifying its
 * citations. Mirrored server-side by the cross-run read model, so the tile
 * and the inbox agree with this tab on what "every quote matches" means.
 */
import { TERMINAL_STATUSES, type RunStatus } from "@/api/types";
import { formatRelative } from "@/lib/format";

import type { Challenge, Citation, Finding, Reproduction, ReviewOutcomeValue } from "./api";

/** The seven filters the tally chips offer, in the order they render. */
export type ChipId =
  | "quote_matches"
  | "stale"
  | "quote_not_found"
  | "never_read"
  | "read_not_observed"
  | "not_checkable"
  | "declared_only";

interface Chip {
  id: ChipId;
  label: string;
}

/**
 * The one source of every verdict's wording (18-UI-SPEC Copywriting Contract): `chip`, the
 * tally chip and inbox filter label; `badge`, the `VerdictBadge` phrase; `tally`, the phrase
 * in a tally sentence. The forms differ on purpose (a chip says "Cannot check", a sentence
 * says "could not be checked").
 */
export const VERDICT_LABEL: Record<
  ChipId,
  { chip: string; badge: string; tally: (n: number) => string }
> = {
  quote_matches: {
    chip: "Quote matches",
    badge: "Quote matches",
    tally: (n) => (n === 1 ? "1 quote matches" : `${n} quotes match`),
  },
  stale: {
    chip: "Stale",
    badge: "Stale: file changed after this read",
    tally: (n) => `${n} stale`,
  },
  quote_not_found: {
    chip: "Quote not found",
    badge: "Quote not found in recorded lines",
    tally: (n) => `${n} quote not found`,
  },
  never_read: { chip: "Never read", badge: "Never read", tally: (n) => `${n} never read` },
  read_not_observed: {
    chip: "Read not observed",
    badge: "Read not observed",
    tally: (n) => `${n} read not observed`,
  },
  not_checkable: {
    chip: "Cannot check",
    badge: "Cannot check",
    tally: (n) => `${n} could not be checked`,
  },
  declared_only: {
    chip: "Declared only",
    badge: "Declared only, no citation given",
    tally: (n) => `${n} declared only`,
  },
};

/** The six verdicts in chip and tally-sentence order; declared only is not a verdict. */
const VERDICT_ORDER = [
  "quote_matches",
  "stale",
  "quote_not_found",
  "never_read",
  "read_not_observed",
  "not_checkable",
] as const;

export const CHIPS: readonly Chip[] = [...VERDICT_ORDER, "declared_only" as const].map((id) => ({
  id,
  label: VERDICT_LABEL[id].chip,
}));

/** A citation whose latest verdict is still null (not yet decided) counts here. */
export type WorstVerdict = ChipId | "unchecked";

/** Every status `VerdictBadge` can show: a verdict, declared only, or a pending state. */
export type BadgeStatus = ChipId | PendingState;

/** The badge for a finding's worst verdict: an undecided one reads "Checked when the run
 * finishes" while the run is live and "Not checked yet" once it is not. */
export function badgeStatus(worst: WorstVerdict, live: boolean): BadgeStatus {
  if (worst !== "unchecked") return worst;
  return live ? "checked_when_finished" : "not_checked_yet";
}

/** Worst first: a finding's own worst citation decides its list badge and inbox sort. */
const VERDICT_RANK: Record<WorstVerdict, number> = {
  quote_not_found: 0,
  never_read: 1,
  stale: 2,
  not_checkable: 3,
  read_not_observed: 4,
  unchecked: 5,
  declared_only: 6,
  quote_matches: 7,
};

export type CitationTally = Record<ChipId, number> & { unchecked: number };

/** Tallies every citation's latest verdict across the given findings, plus findings with
 * zero citations (declared only) and citations whose verdict has not been decided yet. */
export function citationTally(findings: readonly Finding[]): CitationTally {
  const tally: CitationTally = {
    quote_matches: 0,
    stale: 0,
    quote_not_found: 0,
    never_read: 0,
    read_not_observed: 0,
    not_checkable: 0,
    declared_only: 0,
    unchecked: 0,
  };
  for (const finding of findings) {
    if (finding.citations.length === 0) {
      tally.declared_only += 1;
      continue;
    }
    for (const citation of finding.citations) {
      const value = citation.verdict?.verdict;
      if (value === undefined) tally.unchecked += 1;
      else tally[value] += 1;
    }
  }
  return tally;
}

/** A tally from the backend's per-verdict citation counts, where only non-zero keys are
 * present (the Overview tile's `verdicts` and an inbox row's `verdict_counts`): a missing
 * key counts 0. The caller adds `declared_only` and `unchecked` to `counts` when its
 * payload carries them separately. */
export function tallyFromCounts(counts: Readonly<Record<string, number>>): CitationTally {
  const tally = {} as CitationTally;
  for (const id of [...VERDICT_ORDER, "declared_only", "unchecked"] as const) {
    tally[id] = counts[id] ?? 0;
  }
  return tally;
}

/** The comma-separated tally phrases; a zero count is omitted; declared-only findings are
 * counted separately and always come last. */
export function tallyPhrases(tally: CitationTally): string {
  const parts: string[] = [];
  for (const id of VERDICT_ORDER) {
    if (tally[id] > 0) parts.push(VERDICT_LABEL[id].tally(tally[id]));
  }
  if (tally.unchecked > 0) parts.push(`${tally.unchecked} not checked yet`);
  if (tally.declared_only > 0) parts.push(VERDICT_LABEL.declared_only.tally(tally.declared_only));
  return parts.join(", ");
}

/** A finding with no citations is declared only; otherwise the worst rank among its
 * citations' latest verdicts (a null verdict ranks as "unchecked"). */
export function worstVerdict(finding: Finding): WorstVerdict {
  if (finding.citations.length === 0) return "declared_only";
  let worst: WorstVerdict = "quote_matches";
  for (const citation of finding.citations) {
    const value: WorstVerdict = citation.verdict?.verdict ?? "unchecked";
    if (VERDICT_RANK[value] < VERDICT_RANK[worst]) worst = value;
  }
  return worst;
}

/** True when any of the finding's citations carries the chip's verdict as its latest one;
 * the declared-only chip matches a finding with zero citations. */
export function findingMatchesChip(finding: Finding, chip: ChipId): boolean {
  if (chip === "declared_only") return finding.citations.length === 0;
  return finding.citations.some((citation) => citation.verdict?.verdict === chip);
}

/** A cheap product signal: whether a citation's retained read is anchored to a Git
 * blob in the run's own repository, or retained only from what the agent itself reported
 * (an observed agent can fabricate hook/MCP telemetry, so a `quote_matches` verdict on
 * reported-only content proves consistency with what was reported, not with the
 * repository). A Git blob only shows the object exists (`git cat-file -e`); anything that
 * can write the repository could have added it (`git hash-object -w`), so the label never
 * claims the lines are committed content. `blobRef` is `verdict.evidence.read.blob_ref` or a citation content
 * response's `blob_ref`: `git:<id>` or `sha256:<hex>`. An unrecognized prefix falls back to
 * a plain label with no caveat, so an unknown form is never misdescribed as one or the
 * other. */
export function readProvenance(blobRef: string | null): { label: string; caveat?: string } | null {
  if (blobRef === null) return null;
  if (blobRef.startsWith("git:")) {
    return {
      label: `Git blob ${blobRef.slice(4, 11)}`,
      caveat:
        "Stored as a Git object in the run's repository, which AgentTape checked exists. Anything that can write to the repository can add such an object, so this is not proof the lines were committed.",
    };
  }
  if (blobRef.startsWith("sha256:")) {
    return {
      label: `Reported output ${blobRef.slice(7, 14)}`,
      caveat:
        "Retained from the output the agent reported, not anchored to a Git blob: it shows what was reported, not what the repository contained.",
    };
  }
  return { label: blobRef.slice(0, 10) };
}

/** True for a verdict the demo seed wrote rather than the verification engine; its
 * surfaces say "Seeded demo stand-in" instead of presenting it as a real check. */
export function isSeeded(verdict: { method?: string | null } | null | undefined): boolean {
  return verdict?.method === "seeded";
}

type PendingState = "checked_when_finished" | "checking" | "not_checked_yet";

/** A citation still stays "checked when the run finishes" while the run is live, becomes
 * "checking" for 30 seconds after the run finishes, then "not checked yet". A citation
 * that already carries a verdict has no pending state (null). */
export function pendingState(
  run: { status: RunStatus; finished_at: string | null },
  citation: Citation,
  now: number,
): PendingState | null {
  if (citation.verdict !== null) return null;
  if (!TERMINAL_STATUSES.has(run.status)) return "checked_when_finished";
  if (!run.finished_at) return "not_checked_yet";
  const elapsedMs = now - new Date(run.finished_at).getTime();
  return elapsedMs < 30_000 ? "checking" : "not_checked_yet";
}

/** The statement card's four frames: live, all-declared-only, verdicts still arriving, and
 * the normal terminal statement with its tally phrases and the checkable-not-foolproof
 * caveat. The caller appends the file-evidence completeness sentence separately. */
export function statementText(findings: readonly Finding[], live: boolean): string {
  const totalCitations = findings.reduce((sum, finding) => sum + finding.citations.length, 0);
  if (totalCitations === 0) {
    return `This run recorded ${findings.length} findings and none of them cites lines or a recorded event, so there is nothing to check.`;
  }
  if (live) {
    return "This run is still recording. Citations are checked once it finishes.";
  }
  const tally = citationTally(findings);
  const decided = totalCitations - tally.unchecked;
  if (decided === 0) {
    return `Checking ${totalCitations} citations against the record.`;
  }
  const phrases = tallyPhrases(tally);
  return `AgentTape checked ${totalCitations} citations in ${findings.length} findings against what it recorded during this run: ${phrases}. Checkable, not foolproof: a matching quote shows the lines existed when they were read, not that the conclusion is right.`;
}

/* ------------------------------------------------------------------------------------- */
/* The evidence ladder: six fixed slots, weakest to strongest, always in this     */
/* order. The Challenge rung (5) is model judgment, never proof, and never renders in the  */
/* success/danger tones the citation verdicts use; `violet` is reserved for it.           */
/* ------------------------------------------------------------------------------------- */

type RungSlot = 1 | 2 | 3 | 4 | 5 | 6;

/** lucide-react component name; `EvidenceLadder` maps this to the actual icon so this
 * module stays free of a React/lucide dependency and is cheaply unit-testable. */
export type RungIcon =
  "MessageSquareQuote" | "Check" | "SquareTerminal" | "Repeat" | "Scale" | "UserCheck";

/** The rung's colour when `achieved`; an unachieved rung always renders outlined
 * (`border-line text-faint`) regardless of `tone`. */
export type RungTone = "neutral" | "success" | "violet" | "accent";

export interface Rung {
  slot: RungSlot;
  /** The rung's stable name (the UI-SPEC "Rung explanations" names), independent of its
   * `label`, which changes with state (for example "Quotes match (2 of 2)"). */
  name: string;
  label: string;
  achieved: boolean;
  tone: RungTone;
  icon: RungIcon;
  /** Shown below the ladder when this rung's button is pressed (UI-SPEC "Rung explanations";
   * this is what the missing Tooltip primitive would have said). */
  explanation: string;
  /** Present only when this rung has a child run to open (the Challenge rung, every form but
   * "Not challenged"). */
  href?: string;
}

const RUNG_NAME: Record<RungSlot, string> = {
  1: "Agent declared",
  2: "Quotes match",
  3: "Event observed",
  4: "Reproduced",
  5: "Challenge",
  6: "Confirmed",
};

const RUNG_ICON: Record<RungSlot, RungIcon> = {
  1: "MessageSquareQuote",
  2: "Check",
  3: "SquareTerminal",
  4: "Repeat",
  5: "Scale",
  6: "UserCheck",
};

const RUNG_EXPLANATION: Record<RungSlot, string> = {
  1: "The agent recorded this finding. That is a statement, not evidence.",
  2: "Each cited quote appears in what this run read or recorded, at the matched lines. It does not show the claim is true.",
  3: "The finding cites a recorded command or tool result, and the quote appears in its output.",
  4: "AgentTape re-ran the cited command at the same commit and got the same answer. It re-runs only read-only searches automatically.",
  5: "A second agent read the same evidence and gave its opinion. This is model judgment and never counts as proof.",
  6: "A person reviewed this finding and confirmed it. The confirmation is in the audit log.",
};

function rung1Declared(): Rung {
  return {
    slot: 1,
    name: RUNG_NAME[1],
    label: "Agent declared",
    achieved: true,
    tone: "neutral",
    icon: RUNG_ICON[1],
    explanation: RUNG_EXPLANATION[1],
  };
}

/** Every citation counts (file and event alike): a claim with an event-only citation still
 * needs its quote to match before rung 2 is achieved. */
function rung2QuotesMatch(finding: Finding): Rung {
  const cited = finding.citations.length;
  const base = {
    slot: 2 as const,
    name: RUNG_NAME[2],
    tone: "success" as const,
    icon: RUNG_ICON[2],
    explanation: RUNG_EXPLANATION[2],
  };
  if (cited === 0) {
    return { ...base, label: "Quotes match: no citation", achieved: false };
  }
  const matched = finding.citations.filter((c) => c.verdict?.verdict === "quote_matches").length;
  if (matched === cited) {
    return { ...base, label: `Quotes match (${matched} of ${cited})`, achieved: true };
  }
  return { ...base, label: `Quotes match: ${matched} of ${cited}`, achieved: false };
}

/** The third, honest label below covers every event citation whose latest verdict is not
 * `quote_matches` (including `quote_not_found`, a pending check, or any other verdict): the
 * two-state UI-SPEC copy ("No event cited") would be false for a finding that does cite an
 * event but has not (yet, or ever) had that quote confirmed. */
function rung3EventObserved(finding: Finding): Rung {
  const base = {
    slot: 3 as const,
    name: RUNG_NAME[3],
    tone: "success" as const,
    icon: RUNG_ICON[3],
    explanation: RUNG_EXPLANATION[3],
  };
  const eventCitations = finding.citations.filter((c) => c.kind === "event");
  if (eventCitations.length === 0) {
    return { ...base, label: "No event cited", achieved: false };
  }
  const observed = eventCitations.some((c) => c.verdict?.verdict === "quote_matches");
  if (observed) {
    return { ...base, label: "Event observed", achieved: true };
  }
  return { ...base, label: "Event cited, quote not matched", achieved: false };
}

/** The strongest reproduction result across the finding's citations wins: reproduced beats a
 * still-queued/running re-run, which beats a decided failure to reproduce. */
function rung4Reproduced(finding: Finding): Rung {
  const base = {
    slot: 4 as const,
    name: RUNG_NAME[4],
    tone: "success" as const,
    icon: RUNG_ICON[4],
    explanation: RUNG_EXPLANATION[4],
  };
  const reproductions: Reproduction[] = finding.citations
    .map((c) => c.reproduction)
    .filter((r): r is Reproduction => r !== null);
  if (reproductions.length === 0) {
    return { ...base, label: "Not re-run", achieved: false };
  }
  if (reproductions.some((r) => r.result === "reproduced")) {
    return { ...base, label: "Reproduced by AgentTape", achieved: true };
  }
  if (reproductions.some((r) => r.status === "queued" || r.status === "running")) {
    return { ...base, label: "Re-run queued", achieved: false };
  }
  if (reproductions.some((r) => r.result === "not_reproduced")) {
    return { ...base, label: "Not reproduced", achieved: false };
  }
  return { ...base, label: "Could not run", achieved: false };
}

function outcomeLabel(outcome: ReviewOutcomeValue): string {
  switch (outcome) {
    case "upheld":
      return "Challenge: upheld (model judgment)";
    case "refuted":
      return "Challenge: refuted (model judgment)";
    case "inconclusive":
      return "Challenge: inconclusive (model judgment)";
  }
}

/** The newest challenge by its child run's `created_at` (zero-one-many: rung 5 always
 * reflects only this one, the Challenges section lists every challenge). */
function newestChallenge(challenges: readonly Challenge[]): Challenge | null {
  if (challenges.length === 0) return null;
  return challenges.reduce((newest, candidate) =>
    new Date(candidate.run.created_at).getTime() > new Date(newest.run.created_at).getTime()
      ? candidate
      : newest,
  );
}

/** Violet only for a completed outcome that actually reviewed this finding; upheld and
 * refuted read identically: a second agent's opinion is never proof either way. */
function rung5Challenge(finding: Finding, challenges: readonly Challenge[]): Rung {
  const base = {
    slot: 5 as const,
    name: RUNG_NAME[5],
    icon: RUNG_ICON[5],
    explanation: RUNG_EXPLANATION[5],
  };
  const newest = newestChallenge(challenges);
  if (!newest) {
    return { ...base, label: "Not challenged (model judgment)", achieved: false, tone: "violet" };
  }
  const href = `/runs/${newest.run.id}`;
  if (newest.state === "pending") {
    return {
      ...base,
      label: "Challenge running (model judgment)",
      achieved: true,
      tone: "neutral",
      href,
    };
  }
  if (newest.state === "did_not_complete") {
    return {
      ...base,
      label: "Challenge did not complete (model judgment)",
      achieved: true,
      tone: "neutral",
      href,
    };
  }
  const outcome = newest.outcomes.find((o) => o.target_finding_key === finding.key);
  if (!outcome) {
    return {
      ...base,
      label: "Challenge: not reviewed (model judgment)",
      achieved: false,
      tone: "violet",
      href,
    };
  }
  return { ...base, label: outcomeLabel(outcome.outcome), achieved: true, tone: "violet", href };
}

function rung6Confirmed(finding: Finding, now: number): Rung {
  const base = {
    slot: 6 as const,
    name: RUNG_NAME[6],
    tone: "accent" as const,
    icon: RUNG_ICON[6],
    explanation: RUNG_EXPLANATION[6],
  };
  const confirmation = finding.confirmation;
  if (!confirmation) {
    return { ...base, label: "Not confirmed", achieved: false };
  }
  return {
    ...base,
    label: `Confirmed by ${confirmation.actor}, ${formatRelative(confirmation.ts, now)}`,
    achieved: true,
  };
}

/** The six evidence rungs for one finding, always in this fixed order. `now` (epoch ms)
 * decides rung 6's relative-time phrase; pass `Date.now()` from a re-rendering caller so it
 * keeps advancing, or a fixed value in a test. */
export function ladder(
  finding: Finding,
  challenges: readonly Challenge[],
  now: number,
): [Rung, Rung, Rung, Rung, Rung, Rung] {
  return [
    rung1Declared(),
    rung2QuotesMatch(finding),
    rung3EventObserved(finding),
    rung4Reproduced(finding),
    rung5Challenge(finding, challenges),
    rung6Confirmed(finding, now),
  ];
}
