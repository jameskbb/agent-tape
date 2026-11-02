/**
 * The run header's "Findings" fact: the finding tally after the Events fact on
 * every tab, its whole value linking to `?tab=findings`. Uses `useFindings`'s shared query
 * cache (`findingsKey`) so the header, the Findings tab count, and the tab itself never
 * fetch findings more than once for the same run.
 */
import { Link, useSearchParams } from "react-router";

import { TERMINAL_STATUSES, type RunDetail } from "@/api/types";
import { useAdapterDetails } from "@/features/agents/api";

import { useFindings } from "./api";
import { findingsCapability, recordsFindings } from "./capability";
import { citationTally, tallyPhrases } from "./model";

export function RunHeaderTally({ detail }: { detail: RunDetail }) {
  const { run } = detail;
  const [params] = useSearchParams();
  const live = !TERMINAL_STATUSES.has(run.status);
  const query = useFindings(run.id, live, run.finished_at);
  const adapters = useAdapterDetails();

  if (!recordsFindings(run)) return null;
  if (!query.isSuccess) return null;

  const findings = query.data;
  if (findings.length === 0 && findingsCapability(run, adapters.data) === "cannot") return null;

  const value =
    findings.length === 0 ? "0" : `${findings.length}: ${tallyPhrases(citationTally(findings))}`;

  const search = new URLSearchParams(params);
  search.set("tab", "findings");

  return (
    <div className="min-w-0">
      <dt className="text-xs text-muted">Findings</dt>
      <dd className="mt-0.5 truncate text-sm text-ink">
        <Link
          to={{ search: `?${search.toString()}` }}
          title={value}
          className="text-accent hover:underline"
        >
          {value}
        </Link>
      </dd>
    </div>
  );
}
