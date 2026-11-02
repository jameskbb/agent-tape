/**
 * The Context tab's compact findings card: one row per finding with its kind,
 * worst verdict, clamped claim, and its Supports / Conflicts / Missing source chips, each
 * row linking to the finding on the Findings tab, where its cited lines live. This card
 * never renders `CitationView` itself.
 */
import { Link } from "react-router";

import { Badge, Card, ErrorState, LoadingBlock } from "@/components/ui";
import { useFindings, type Finding } from "@/features/findings/api";
import { badgeStatus, worstVerdict } from "@/features/findings/model";
import { VerdictBadge } from "@/features/findings/VerdictBadge";
import { humanize } from "@/lib/format";

import { SourceChips } from "./SourceChips";

const enc = encodeURIComponent;

function FindingRow({
  runId,
  finding,
  live,
  onOpen,
}: {
  runId: string;
  finding: Finding;
  live: boolean;
  onOpen: (id: string | null) => void;
}) {
  return (
    <li className="rounded border border-line p-3" data-testid="finding">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <Badge tone="accent">{humanize(finding.kind)}</Badge>
        <VerdictBadge verdict={badgeStatus(worstVerdict(finding), live)} compact />
      </div>
      <p className="mt-1.5 line-clamp-2 font-medium break-words text-ink">{finding.text}</p>
      <div className="mt-2 flex flex-col gap-1.5">
        <SourceChips label="Supports" items={finding.supports} tone="success" onOpen={onOpen} />
        <SourceChips label="Conflicts" items={finding.conflicts} tone="danger" onOpen={onOpen} />
        <SourceChips label="Missing" items={finding.missing} tone="warning" onOpen={onOpen} />
      </div>
      <Link
        to={`/runs/${enc(runId)}?tab=findings&finding=${enc(finding.key)}`}
        className="mt-2 inline-block text-sm text-accent hover:underline"
      >
        Open on Findings tab
      </Link>
    </li>
  );
}

export function FindingsCard({
  runId,
  live,
  onOpen,
}: {
  runId: string;
  live: boolean;
  onOpen: (id: string | null) => void;
}) {
  const findings = useFindings(runId, live);
  if (findings.isPending) return <LoadingBlock label="Loading findings" />;
  if (findings.isError) {
    return <ErrorState error={findings.error} onRetry={() => void findings.refetch()} />;
  }
  return (
    <Card
      title={`Findings (${findings.data.length})`}
      description="Kind, verdict, and the sources each finding links to. Open a finding to see its cited lines."
    >
      {findings.data.length === 0 ? (
        <p className="text-sm text-muted">The agent reported no structured findings.</p>
      ) : (
        <ul className="flex flex-col gap-3">
          {findings.data.map((finding) => (
            <FindingRow
              key={finding.key}
              runId={runId}
              finding={finding}
              live={live}
              onOpen={onOpen}
            />
          ))}
        </ul>
      )}
    </Card>
  );
}
