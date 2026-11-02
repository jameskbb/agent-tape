/**
 * The Findings detail pane: the claim in full, the partial-payload notes, the evidence
 * ladder, one `CitationView` per citation in position order, the Challenges section (only
 * when at least one exists), and last the Confirm control (only for a stored finding).
 */
import { Link } from "react-router";

import type { RunDetail, RunEvent } from "@/api/types";
import { Badge, RelativeTime, StatusPill } from "@/components/ui";
import { humanize } from "@/lib/format";

import { useChallenges, type Challenge, type Finding } from "./api";
import { CitationView } from "./CitationView";
import { ConfirmControl } from "./ConfirmControl";
import { EvidenceLadder } from "./EvidenceLadder";

/** One row per challenge, newest first; the outcome text never carries a success/danger
 * tone; it is plain text naming the outcome and, when this finding was not part
 * of that challenge's outcomes, saying so honestly instead of guessing. */
function ChallengeRow({ challenge, finding }: { challenge: Challenge; finding: Finding }) {
  const outcome = challenge.outcomes.find((o) => o.target_finding_key === finding.key);
  const phrase = outcome
    ? `${outcome.outcome} for this finding (model judgment)`
    : "not reviewed (finding omitted)";
  return (
    <li className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
      <Link to={`/runs/${challenge.run.id}`} className="text-accent hover:underline">
        {challenge.run.title}
      </Link>
      <StatusPill status={challenge.run.status} size="sm" />
      <span className="text-muted">
        started <RelativeTime iso={challenge.run.created_at} />: {phrase}
      </span>
    </li>
  );
}

function ChallengesSection({ challenges, finding }: { challenges: Challenge[]; finding: Finding }) {
  if (challenges.length === 0) return null;
  const sorted = [...challenges].sort(
    (a, b) => new Date(b.run.created_at).getTime() - new Date(a.run.created_at).getTime(),
  );
  return (
    <div className="flex flex-col gap-1.5">
      <h4 className="text-xs font-semibold text-muted">Challenges ({challenges.length})</h4>
      <ul className="flex flex-col gap-1.5">
        {sorted.map((challenge) => (
          <ChallengeRow key={challenge.run.id} challenge={challenge} finding={finding} />
        ))}
      </ul>
    </div>
  );
}

export function FindingDetail({
  finding,
  detail,
  events,
}: {
  finding: Finding;
  detail: RunDetail;
  events: RunEvent[];
}) {
  const citationsDropped = finding.details["citations_dropped"];
  const payloadTruncated = finding.details["payload_truncated"];
  const citations = [...finding.citations].sort((a, b) => a.position - b.position);
  const challenges = useChallenges(detail.run.id);
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col items-start gap-1">
        <Badge tone="accent">{humanize(finding.kind)}</Badge>
        <h3 className="text-lg leading-[1.35] font-medium break-words text-ink">{finding.text}</h3>
        {typeof citationsDropped === "number" && citationsDropped > 0 ? (
          <p className="text-sm text-muted">
            {citationsDropped} citations past the 50-citation cap were not recorded
          </p>
        ) : null}
        {payloadTruncated === true ? (
          <p className="text-sm text-muted">The finding payload was cut at its size cap</p>
        ) : null}
      </div>
      <div className="flex flex-col gap-1.5">
        <h4 className="text-xs font-semibold text-muted">Evidence for this finding</h4>
        <EvidenceLadder finding={finding} challenges={challenges.data ?? []} />
      </div>
      {citations.length === 0 ? (
        <p className="text-sm text-ink-soft">
          {
            "No citation given. AgentTape can only check a claim that cites lines or a recorded event."
          }
        </p>
      ) : (
        <div className="flex flex-col gap-4">
          {citations.map((citation) => (
            <CitationView key={citation.id} citation={citation} run={detail.run} events={events} />
          ))}
        </div>
      )}
      <ChallengesSection challenges={challenges.data ?? []} finding={finding} />
      {finding.id ? (
        <div className="mt-6">
          <ConfirmControl finding={finding} runId={detail.run.id} />
        </div>
      ) : null}
    </div>
  );
}
