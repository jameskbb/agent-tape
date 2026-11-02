/**
 * The Findings run-detail tab: what AgentTape checked (the statement card and the tally
 * chips that filter the list), a query-backed tab count, Re-check citations, the
 * recorded-order list with keyboard selection, the stacked picker below `lg`, and the
 * detail pane.
 */
import { RefreshCw } from "lucide-react";
import { useSearchParams } from "react-router";

import { describeError } from "@/api/client";
import { TERMINAL_STATUSES, type RunDetail, type RunEvent } from "@/api/types";
import {
  Button,
  Card,
  EmptyState,
  ErrorState,
  LoadingBlock,
  TabCount,
  useToast,
} from "@/components/ui";
import { useAdapterDetails } from "@/features/agents/api";
import { useRunFileSummary } from "@/features/files/api";
import { cn } from "@/lib/cn";

import { useFindings, useRecheckCitations } from "./api";
import { adapterLabel, findingsCapability, FINDINGS_DOC_URL } from "./capability";
import { FindingDetail } from "./FindingDetail";
import { FindingList, FindingPicker } from "./FindingList";
import { CHIPS, citationTally, findingMatchesChip, statementText, type ChipId } from "./model";

/** The Findings tab's `count`: the finding count once loaded, absent while loading
 * or on error, and absent when the adapter cannot record findings and none were found (the
 * tab then shows the "No findings recorded" empty state, not a "0"). Shares the page's
 * findings query cache with `FindingsTab` itself. */
export function FindingsTabCount({ detail }: { detail: RunDetail }) {
  const live = !TERMINAL_STATUSES.has(detail.run.status);
  const query = useFindings(detail.run.id, live, detail.run.finished_at);
  const adapters = useAdapterDetails();
  if (!query.isSuccess) return null;
  if (query.data.length === 0 && findingsCapability(detail.run, adapters.data) === "cannot") {
    return null;
  }
  return <TabCount value={query.data.length} />;
}

export function FindingsTab({
  detail,
  events,
  live,
}: {
  detail: RunDetail;
  events: RunEvent[];
  live: boolean;
}) {
  const query = useFindings(detail.run.id, live, detail.run.finished_at);
  const adapters = useAdapterDetails();
  const fileSummary = useRunFileSummary(detail.run.id, live);
  const [params, setParams] = useSearchParams();
  const toast = useToast();
  const recheck = useRecheckCitations(detail.run.id);

  if (query.isPending) return <LoadingBlock label="Loading findings" />;
  if (query.isError) {
    return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  }

  const findings = query.data;

  if (findings.length === 0) {
    const capability = findingsCapability(detail.run, adapters.data);
    if (capability === "cannot") {
      const label = adapterLabel(detail.run, adapters.data);
      return (
        <EmptyState
          title="No findings recorded"
          description={
            <>
              {label} does not record findings. Claude Code records them through the AgentTape MCP
              server (<code>record_finding</code>): launched runs get it automatically; for
              interactive sessions run <code>agenttape mcp register</code>.
            </>
          }
          action={
            <a
              href={FINDINGS_DOC_URL}
              target="_blank"
              rel="noreferrer"
              className="text-sm text-accent hover:underline"
            >
              How findings are recorded
            </a>
          }
        />
      );
    }
    if (capability === "observed") {
      return (
        <EmptyState
          title="This session recorded no findings."
          description="Findings from an interactive Claude Code session arrive through the AgentTape MCP server (agenttape mcp register)."
        />
      );
    }
    return (
      <EmptyState
        title="This run recorded no findings."
        description="The agent records a finding with the record_finding tool; nothing was recorded during this run."
      />
    );
  }

  const tally = citationTally(findings);
  const verdictParam = params.get("verdict");
  const activeChip = CHIPS.find((chip) => chip.id === verdictParam) ?? null;
  const filteredFindings = activeChip
    ? findings.filter((finding) => findingMatchesChip(finding, activeChip.id))
    : findings;

  const selectedKey = params.get("finding") ?? filteredFindings[0]?.key ?? findings[0]?.key ?? null;
  const selected =
    findings.find((finding) => finding.key === selectedKey) ?? filteredFindings[0] ?? null;

  const setParam = (updates: Record<string, string | null>) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        for (const [key, value] of Object.entries(updates)) {
          if (value === null) next.delete(key);
          else next.set(key, value);
        }
        return next;
      },
      { replace: true },
    );

  const select = (key: string) => setParam({ finding: key });
  const toggleChip = (chip: ChipId) => setParam({ verdict: activeChip?.id === chip ? null : chip });
  const clearFilter = () => setParam({ verdict: null });

  let statement = statementText(findings, live);
  if (fileSummary.data && fileSummary.data.completeness.level !== "complete") {
    statement = `${statement} ${fileSummary.data.completeness.statement}`;
  }

  return (
    <div className="flex flex-col gap-4">
      <Card
        title="Checked against the record"
        description={statement}
        className="max-w-[72ch]"
        actions={
          <Button
            variant="secondary"
            size="sm"
            icon={<RefreshCw className="size-4" aria-hidden />}
            disabled={live || recheck.isPending}
            title={live ? "Citations are checked once the run finishes" : undefined}
            loading={recheck.isPending}
            onClick={() =>
              recheck.mutate(undefined, {
                onSuccess: (summary) =>
                  toast.show(
                    `Citations re-checked: ${summary.recorded} verdicts recorded`,
                    "success",
                  ),
                onError: (error) => toast.show(describeError(error), "error"),
              })
            }
          >
            Re-check citations
          </Button>
        }
      >
        <div className="flex flex-wrap items-center gap-2">
          <div
            role="group"
            aria-label="Filter by verdict"
            className="flex flex-wrap items-center gap-2"
          >
            {CHIPS.map((chip) => {
              const count = tally[chip.id];
              const pressed = activeChip?.id === chip.id;
              return (
                <button
                  key={chip.id}
                  type="button"
                  aria-pressed={pressed}
                  disabled={count === 0}
                  onClick={() => toggleChip(chip.id)}
                  className={cn(
                    "inline-flex items-center gap-1 rounded-[4px] border px-1.5 py-px text-xs font-medium whitespace-nowrap disabled:opacity-55",
                    pressed
                      ? "border-transparent bg-accent-soft text-accent"
                      : "border-line bg-sunken text-ink-soft",
                  )}
                >
                  {chip.label} {count}
                </button>
              );
            })}
          </div>
          {activeChip ? (
            <button
              type="button"
              onClick={clearFilter}
              className="text-sm text-accent hover:underline"
            >
              Clear filter
            </button>
          ) : null}
        </div>
        {activeChip ? (
          <p className="mt-2 text-sm text-muted">
            Showing {filteredFindings.length} of {findings.length} findings with {activeChip.label}
          </p>
        ) : null}
      </Card>

      <h3 className="text-sm font-semibold text-muted">Findings ({findings.length})</h3>

      <div className="flex flex-col gap-4 lg:grid lg:grid-cols-12">
        <div className="hidden lg:col-span-5 lg:block">
          <FindingList
            findings={filteredFindings}
            selectedKey={selected?.key ?? null}
            onSelect={select}
            live={live}
          />
        </div>
        <div className="lg:hidden">
          <FindingPicker
            findings={filteredFindings}
            selectedKey={selected?.key ?? null}
            onSelect={select}
          />
        </div>
        <div className="flex flex-col gap-4 lg:col-span-7">
          {selected ? (
            <FindingDetail finding={selected} detail={detail} events={events} />
          ) : (
            <EmptyState
              title="No finding selected"
              description="Choose a finding from the list to see its citations."
            />
          )}
        </div>
      </div>
    </div>
  );
}
