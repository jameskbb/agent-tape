/**
 * The Overview's first tile: how many findings in the window had every citation's
 * quote match, defined in this tile's own detail sentence; the word "verified" appears
 * nowhere else in the app (UI-SPEC vocabulary rule). Never renders a fabricated 0 for an
 * empty window; the same window and verdict vocabulary as the Findings inbox.
 */
import { Link } from "react-router";

import { Stat } from "@/components/ui";

import type { FindingsTally } from "./api";
import { FINDINGS_DOC_URL } from "./capability";
import { tallyFromCounts, tallyPhrases } from "./model";

export function VerifiedFindingsStat({ tally, days }: { tally: FindingsTally; days: number }) {
  if (tally.recorded === 0) {
    return (
      <Stat
        label="Verified findings"
        value={<span className="text-base font-normal text-muted">No findings</span>}
        detail={
          <>
            <span>{`No findings recorded in the last ${days} days.`}</span>{" "}
            <a
              href={FINDINGS_DOC_URL}
              target="_blank"
              rel="noreferrer"
              className="text-accent underline"
            >
              How findings are recorded
            </a>
          </>
        }
      />
    );
  }
  const phrases = tallyPhrases(
    tallyFromCounts({
      ...tally.verdicts,
      declared_only: tally.declared_only,
      unchecked: tally.unchecked,
    }),
  );
  return (
    <Stat
      label="Verified findings"
      value={tally.all_quotes_match}
      detail={
        <>
          <span>{`Every quote matched in ${tally.all_quotes_match} of ${tally.recorded} findings in the last ${days} days: ${phrases}.`}</span>{" "}
          <Link to={`/findings?days=${days}`} className="text-accent underline">
            All findings
          </Link>
        </>
      }
    />
  );
}
