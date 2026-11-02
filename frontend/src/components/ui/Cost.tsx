import { readCost, formatUsd } from "@/lib/cost";

import { Badge } from "./Badge";
import { Unavailable } from "./Unavailable";

/** Formatted reported cost, labelled when it is an estimate or simulated; never raw JSON. */
export function Cost({ cost }: { cost: Record<string, unknown> | null | undefined }) {
  const r = readCost(cost);
  if (r.kind === "unavailable") return <Unavailable reason={r.reason} />;
  if (r.kind === "simulated") return <Badge tone="warning">Simulated, not real cost</Badge>;
  return (
    <span className="inline-flex items-center gap-1.5 tabular-nums" title={r.source ?? undefined}>
      {formatUsd(r.amount)}
      {r.estimated ? (
        <Badge tone="neutral" title={r.source ?? undefined}>
          Estimate
        </Badge>
      ) : null}
    </span>
  );
}
