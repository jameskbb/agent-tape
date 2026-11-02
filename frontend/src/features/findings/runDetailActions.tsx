/** "Challenge findings" in the run-detail header (order 5: right after Export, before
 * Replay / Fork / Compare, which register at order 10). */
import { Scale } from "lucide-react";
import { useState } from "react";

import { TERMINAL_STATUSES, type RunDetail } from "@/api/types";
import type { RunDetailAction } from "@/app/registry";
import { Button } from "@/components/ui";

import { useFindings } from "./api";
import { recordsFindings } from "./capability";
import { ChallengeDialog } from "./ChallengeDialog";

function ChallengeFindingsAction({ detail }: { detail: RunDetail }) {
  const { run } = detail;
  const finished = TERMINAL_STATUSES.has(run.status);
  const findings = useFindings(run.id, !finished, run.finished_at);
  const [open, setOpen] = useState(false);
  if (!recordsFindings(run)) return null;
  const findingCount = findings.data?.length ?? 0;
  const disabledReason = !finished
    ? "Challenge findings needs a finished run"
    : findingCount === 0
      ? "This run recorded no findings to challenge"
      : null;
  return (
    <>
      <Button
        variant="secondary"
        icon={<Scale className="size-4" aria-hidden />}
        disabled={disabledReason !== null}
        title={disabledReason ?? undefined}
        onClick={() => setOpen(true)}
      >
        Challenge findings
      </Button>
      {open ? (
        <ChallengeDialog run={run} findingCount={findingCount} onClose={() => setOpen(false)} />
      ) : null}
    </>
  );
}

export const runDetailActions: RunDetailAction[] = [
  {
    id: "challenge-findings",
    order: 5,
    render: ({ detail }) => <ChallengeFindingsAction detail={detail} />,
  },
];
