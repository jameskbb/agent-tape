/**
 * The one component that renders cited lines: the Source drawer links to a
 * citation's Findings tab entry and never embeds a second copy of this viewer.
 *
 * Each file-citation verdict gets its own honest body (never hidden, never collapsed except
 * the raw "Show verdict evidence" disclosure): quote_matches and stale mark the retained
 * lines the verdict was decided from (stale additionally hatched, with the write and both
 * hashes); quote_not_found shows the recorded lines and the agent's quote side by side;
 * never_read and read_not_observed show only the agent's quote, with the danger or neutral
 * sentence; not_checkable shows the agent's quote and its reason. An event citation (or an
 * unchecked file citation) falls back to the plain "Quoted by the agent" block until a later
 * plan gives event citations their own body.
 */
import { Info } from "lucide-react";
import { useId, useState, type ReactNode } from "react";
import { Link } from "react-router";

import { TERMINAL_STATUSES } from "@/api/types";
import type { RunDetail, RunEvent } from "@/api/types";
import {
  Button,
  CodeBlock,
  ErrorState,
  JsonInspector,
  Skeleton,
  Unavailable,
} from "@/components/ui";
import { DiffViewer } from "@/features/changes/DiffViewer";
import { languageFor, tokenize } from "@/features/changes/highlight";
import { cn } from "@/lib/cn";
import { formatOffset, humanize } from "@/lib/format";
import { useNow } from "@/lib/useNow";

import {
  CITATION_CONTENT_MAX_LINES,
  useCitationContent,
  useReproduceCitation,
  type Citation,
  type CitationContent,
  type Reproduction,
  type Verdict,
} from "./api";
import { eventContentOf, numberedLines, parseUnifiedDiff, record } from "./eventContent";
import { isSeeded, pendingState, readProvenance } from "./model";
import { isNotCheckableReason, NOT_CHECKABLE, VerdictBadge } from "./VerdictBadge";

const enc = encodeURIComponent;

function str(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function numberPair(value: unknown): [number, number] | null {
  return Array.isArray(value) &&
    value.length === 2 &&
    typeof value[0] === "number" &&
    typeof value[1] === "number"
    ? [value[0], value[1]]
    : null;
}

function hashText(hash: string | null): string {
  return hash ? hash.slice(0, 10) : "Unavailable";
}

/**
 * A read's provenance label ("Git blob 05f7502", "Reported output 05f7502") with its caveat.
 * The caveat is the label's accessible description, so a screen reader announces it on
 * focus, and the label is a disclosure button that shows the same sentence to keyboard,
 * touch and mouse users (there is no Tooltip primitive; the evidence ladder uses the same
 * pattern). Rendered as direct children of a wrapping flex row: the caveat takes the full
 * width at the end of the row, under everything else in it.
 */
function ProvenanceLabel({ provenance }: { provenance: { label: string; caveat?: string } }) {
  const [open, setOpen] = useState(false);
  const caveatId = useId();
  if (!provenance.caveat) return <span className="text-muted">{provenance.label}</span>;
  return (
    <>
      <button
        type="button"
        aria-expanded={open}
        aria-controls={caveatId}
        aria-describedby={caveatId}
        onClick={() => setOpen(!open)}
        className="inline-flex items-center gap-1 text-muted underline decoration-dotted underline-offset-2 hover:text-ink"
      >
        {provenance.label}
        <Info aria-hidden className="size-3.5" />
      </button>
      <span
        id={caveatId}
        hidden={!open}
        className="order-last basis-full font-sans text-xs text-muted [overflow-wrap:anywhere]"
      >
        {provenance.caveat}
      </span>
    </>
  );
}

/** The header's verdict badge, or the pending badge while no verdict is decided. It
 * re-renders every 3 s while pending, so "Checking" becomes "Not checked yet" once the 30 s
 * polling window closes without a fetch. */
function HeaderBadge({ citation, run }: { citation: Citation; run: RunDetail["run"] }) {
  const verdict = citation.verdict;
  const now = useNow(3000, verdict === null);
  return (
    <span className="ml-auto">
      {verdict ? (
        <VerdictBadge
          verdict={verdict.verdict}
          reasonCode={str(verdict.evidence["not_checkable_reason"])}
          reasonText={verdict.reason}
          method={verdict.method}
        />
      ) : (
        <VerdictBadge verdict={pendingState(run, citation, now) ?? "not_checked_yet"} />
      )}
    </span>
  );
}

function Header({ citation, run }: { citation: Citation; run: RunDetail["run"] }) {
  const verdict = citation.verdict;
  const evidence = verdict?.evidence ?? {};
  const readInfo = record(evidence["read"]);
  const readHash = readInfo ? str(readInfo["blob_ref"]) : null;
  const provenance = readProvenance(readHash);
  const sequence =
    readInfo && typeof readInfo["sequence"] === "number" ? readInfo["sequence"] : null;

  const claimedRange = numberPair(evidence["claimed_range"]);
  const lineRange = numberPair(evidence["line_range"]);
  const rangeMismatch: { claimed: [number, number]; matched: [number, number] } | null =
    evidence["claimed_range_matches"] === false && claimedRange && lineRange
      ? { claimed: claimedRange, matched: lineRange }
      : null;

  const path = citation.path;

  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line bg-sunken px-3 py-2 font-mono text-[0.8125rem]">
      {path ? (
        citation.inside_boundary === false ? (
          <span className="min-w-0 truncate text-ink" title={path}>
            {path}
          </span>
        ) : (
          <Link
            to={`/runs/${run.id}?tab=files&path=${enc(path)}`}
            className="min-w-0 truncate text-accent hover:underline"
            title={path}
          >
            {path}
          </Link>
        )
      ) : (
        <Unavailable reason="No path" />
      )}
      {rangeMismatch ? (
        <span className="text-muted">
          cited L{rangeMismatch.claimed[0]}-L{rangeMismatch.claimed[1]}, matched at L
          {rangeMismatch.matched[0]}-L{rangeMismatch.matched[1]}
        </span>
      ) : citation.line_start !== null ? (
        <span className="text-muted">
          L{citation.line_start}
          {citation.line_end !== null && citation.line_end !== citation.line_start
            ? `-L${citation.line_end}`
            : ""}
        </span>
      ) : null}
      {sequence !== null ? (
        <Link
          to={`/runs/${run.id}?tab=timeline&event=${sequence}`}
          className="text-accent hover:underline"
        >
          read #{sequence}
        </Link>
      ) : null}
      {provenance ? (
        <ProvenanceLabel provenance={provenance} />
      ) : (
        <span className="text-muted">
          <Unavailable reason="No hash" />
        </span>
      )}
      <HeaderBadge citation={citation} run={run} />
    </div>
  );
}

/** "argv joined with spaces" for a command-shaped event, else its own summary (display
 * only, never re-parsed). */
function eventLabel(event: RunEvent): string {
  const argv = event.payload["argv"];
  if (Array.isArray(argv) && argv.every((a) => typeof a === "string")) {
    return argv.join(" ");
  }
  return event.summary;
}

/** An event citation's header: "{event type} #{sequence} {command or summary}" from the
 * in-memory event feed, or the raw `event_ref` when it never resolved to an event. */
function EventHeader({
  citation,
  run,
  event,
}: {
  citation: Citation;
  run: RunDetail["run"];
  event: RunEvent | undefined;
}) {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line bg-sunken px-3 py-2 font-mono text-[0.8125rem]">
      {event ? (
        <span className="min-w-0 truncate text-ink">
          {humanize(event.type)}{" "}
          <Link
            to={`/runs/${run.id}?tab=timeline&event=${event.sequence}`}
            className="text-accent underline"
          >
            #{event.sequence}
          </Link>{" "}
          {eventLabel(event)}
        </span>
      ) : (
        <span className="min-w-0 truncate text-ink" title={citation.event_ref ?? undefined}>
          {citation.event_ref ?? <Unavailable reason="No event reference" />}
        </span>
      )}
      <HeaderBadge citation={citation} run={run} />
    </div>
  );
}

/** The Copywriting Contract's exact "Reproduction line" frames. */
function reproductionText(r: Reproduction): string {
  if (r.status === "queued") return "Re-run queued";
  if (r.status === "running") return "Re-run running";
  const commit = r.pinned_commit ? r.pinned_commit.slice(0, 7) : null;
  const reason = r.reason ?? "";
  const at = commit ? ` at ${commit}` : "";
  if (r.result === "reproduced") return `Re-run reproduced${at}: ${reason}`;
  if (r.result === "not_reproduced") return `Re-run not reproduced${at}: ${reason}`;
  return `Re-run could not run: ${reason}`;
}

/** Under an event citation's header: the reproduction line when one exists, and a
 * "Re-run command" button only when there is none yet and the run is terminal (a file
 * citation, or an event citation with no resolved `event_id`, can never be re-run). */
function ReproductionLine({ citation, run }: { citation: Citation; run: RunDetail["run"] }) {
  const mutation = useReproduceCitation(run.id);
  const reproduction = citation.reproduction;
  const showButton =
    citation.event_id !== null && reproduction === null && TERMINAL_STATUSES.has(run.status);
  if (reproduction === null && !showButton) return null;
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line px-3 py-2 text-sm text-muted">
      <span>{reproduction ? reproductionText(reproduction) : null}</span>
      {showButton ? (
        <Button
          variant="secondary"
          size="sm"
          loading={mutation.isPending}
          onClick={() => mutation.mutate(citation.id)}
        >
          Re-run command
        </Button>
      ) : null}
    </div>
  );
}

/** The `quote_matches` / `quote_not_found` body for an event citation: the recorded
 * event's own content (never the agent's prose), numbered like a file citation's read and
 * marked the same way; a diff-shaped event (`content_key === "diff"`) embeds `DiffViewer`. */
function EventBody({
  citation,
  event,
  verdict,
}: {
  citation: Citation;
  event: RunEvent;
  verdict: Verdict;
}) {
  const { key, content } = eventContentOf(event, verdict.evidence);
  const label = citation.path ?? citation.event_ref ?? event.type;
  if (content === null) {
    // Should not happen for quote_matches/quote_not_found (both require content), but this
    // component never fabricates a body when the recorded event turns out to have none.
    return <AgentBlock citation={citation} />;
  }
  if (key === "diff") {
    const parsed = parseUnifiedDiff(label, content);
    if (parsed) {
      return (
        <div className="p-3">
          <DiffViewer files={[parsed]} />
        </div>
      );
    }
  }
  const lines = numberedLines(content);
  const lineRange = numberPair(verdict.evidence["line_range"]);
  // A read's result over the event text cap is checked against the read's retained output;
  // the event (all this view has) holds only its first part, so say which was checked.
  const retainedNote =
    record(verdict.evidence["event"])?.["retained"] === true ? (
      <p className="px-3 py-2 text-sm text-ink-soft [overflow-wrap:anywhere]">
        Checked against the full retained read (up to 1 MiB). Only the first 16 KiB of this event is
        shown here.
      </p>
    ) : null;
  if (verdict.verdict === "quote_matches") {
    const table = (
      <LinesTable
        lines={lines}
        markRange={lineRange}
        marker="cited"
        ariaLabel={lineRange ? `Lines ${lineRange[0]} to ${lineRange[1]} cited` : undefined}
        path={label}
      />
    );
    if (!retainedNote) return table;
    return (
      <div className="flex flex-col">
        {table}
        {retainedNote}
      </div>
    );
  }
  return (
    <div className="flex flex-col">
      <div className="border-b border-line">
        <LinesTable lines={lines} markRange={null} marker="claimed" path={label} />
      </div>
      {retainedNote}
      <AgentBlock citation={citation} />
    </div>
  );
}

function AgentBlock({ citation }: { citation: Citation }) {
  const truncated = citation.details["quote_truncated"] === true;
  const redacted = citation.details["quote_redacted"] === true;
  const suffix = [truncated ? "(truncated)" : null, redacted ? "(redacted)" : null]
    .filter(Boolean)
    .join(" ");
  return (
    <div className="p-3">
      <CodeBlock
        code={citation.quote ?? ""}
        label="Quoted by the agent"
        className="border-dashed border-line-strong text-muted"
      />
      <p className="mt-1 text-xs text-muted">
        Agent-supplied text, unchecked{suffix ? ` ${suffix}` : ""}
      </p>
    </div>
  );
}

function ContentFooter({ content }: { content: CitationContent }) {
  const lines = [
    content.truncated ? "Retained read cut short at a size cap." : null,
    content.redacted ? "Secrets redacted in this read." : null,
    content.clipped ? clippedNote(content) : null,
  ].filter((line): line is string => line !== null);
  if (lines.length === 0) return null;
  return <p className="border-t border-line px-3 py-1.5 text-xs text-muted">{lines.join(" ")}</p>;
}

/** Why a response stopped short: the endpoint never serves more than
 * `CITATION_CONTENT_MAX_LINES` lines, so say which lines were left out. */
function clippedNote(content: CitationContent): string {
  const max = CITATION_CONTENT_MAX_LINES.toLocaleString("en-US");
  return content.cited
    ? `Showing the first ${max} cited lines; the rest of the cited range is not shown.`
    : `No lines were cited, so this shows the first ${max} lines of the retained read.`;
}

/** How `markRange`'s rows are marked: `cited` (the accent bar and "›"), `stale` (cited,
 * plus the warning hatch), or `claimed` (the danger hatch alone: the range the agent
 * claimed, where its quote was not found). */
type LineMarker = "cited" | "stale" | "claimed";

function LinesTable({
  lines,
  markRange,
  marker,
  ariaLabel,
  path,
}: {
  lines: [number, string][];
  markRange: readonly [number, number] | null;
  marker: LineMarker;
  ariaLabel?: string;
  path: string;
}) {
  const lang = languageFor(path);
  const [start, end] = markRange ?? [null, null];
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse font-mono text-[0.8125rem] leading-5">
        {ariaLabel ? <caption className="sr-only">{ariaLabel}</caption> : null}
        <tbody aria-label={ariaLabel}>
          {lines.map(([lineNo, text]) => {
            const marked = start !== null && lineNo >= start && lineNo <= end;
            const showAccent = marked && marker !== "claimed";
            return (
              <tr
                key={lineNo}
                className={cn(
                  showAccent ? "border-l-[3px] border-accent bg-accent-soft" : "bg-surface",
                  marked && marker === "claimed" ? "at-hatch-danger" : null,
                  marked && marker === "stale" ? "at-hatch-warning" : null,
                )}
              >
                <td className="w-4 border-r border-line px-1 text-center text-accent select-none">
                  {showAccent ? "›" : ""}
                </td>
                <td className="w-12 border-r border-line px-2 text-right text-faint select-none">
                  {lineNo}
                </td>
                <td className="px-3 whitespace-pre">
                  {tokenize(text, lang).map((t, i) => (
                    <span key={i}>{t.text}</span>
                  ))}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** The retained-lines query's loading skeleton, its error with a retry, or the sentence for
 * content that is no longer available; otherwise `children` with the loaded content. */
function ContentState({
  query,
  children,
}: {
  query: ReturnType<typeof useCitationContent>;
  children: (content: CitationContent) => ReactNode;
}) {
  if (query.isPending) {
    return (
      <div className="p-3">
        <Skeleton lines={9} />
      </div>
    );
  }
  if (query.isError) {
    return (
      <div className="p-3">
        <ErrorState
          error={query.error}
          onRetry={() => void query.refetch()}
          title="Could not load the retained lines."
        />
      </div>
    );
  }
  if (query.data.unavailable_reason) {
    return (
      <p className="px-3 py-3 text-sm text-muted">
        The retained content is no longer available; the verdict above was decided when it was.
      </p>
    );
  }
  return children(query.data);
}

/** The `quote_matches` / `stale` body: the retained read the verdict was decided from, the
 * matched rows marked (`content.cited`, already the verdict's matched range), and the
 * "Show 20 more lines" controls bounded by the retained read's own `line_first`/`line_last`. */
function RetainedPanel({ citation, stale }: { citation: Citation; stale?: boolean }) {
  const [win, setWin] = useState({ before: 3, after: 3 });
  const query = useCitationContent(citation.id, win, true);
  return (
    <ContentState query={query}>
      {(content) => (
        <RetainedLines
          content={content}
          stale={stale}
          onMore={(side) => setWin((w) => ({ ...w, [side]: w[side] + 20 }))}
        />
      )}
    </ContentState>
  );
}

function RetainedLines({
  content,
  stale,
  onMore,
}: {
  content: CitationContent;
  stale?: boolean;
  onMore: (side: "before" | "after") => void;
}) {
  const [citedStart, citedEnd] = content.cited ?? [null, null];
  const minLine = content.lines[0]?.[0] ?? null;
  const maxLine = content.lines.at(-1)?.[0] ?? null;
  const atStart = minLine !== null && content.line_first !== null && minLine <= content.line_first;
  const atEnd = maxLine !== null && content.line_last !== null && maxLine >= content.line_last;
  // The endpoint serves at most CITATION_CONTENT_MAX_LINES lines, so asking for more context
  // past that would change nothing on screen: say so instead of offering a dead control.
  const atLimit = content.lines.length >= CITATION_CONTENT_MAX_LINES;
  return (
    <>
      <LinesTable
        lines={content.lines}
        markRange={content.cited}
        marker={stale ? "stale" : "cited"}
        ariaLabel={citedStart !== null ? `Lines ${citedStart} to ${citedEnd} cited` : undefined}
        path={content.path}
      />
      <div className="flex items-center justify-between gap-2 border-t border-line px-3 py-1.5">
        <Button
          variant="ghost"
          size="sm"
          disabled={atStart || atLimit}
          onClick={() => onMore("before")}
        >
          {atStart
            ? "Start of retained read"
            : atLimit
              ? "No more lines above (line limit)"
              : "Show 20 more lines above"}
        </Button>
        <Button
          variant="ghost"
          size="sm"
          disabled={atEnd || atLimit}
          onClick={() => onMore("after")}
        >
          {atEnd
            ? "End of retained read"
            : atLimit
              ? "No more lines below (line limit)"
              : "Show 20 more lines below"}
        </Button>
      </div>
      <ContentFooter content={content} />
    </>
  );
}

/** The `stale` note row: the write that changed the file (named by its own timeline
 * sequence, never File Evidence's separate `change.sequence`) and both hashes. */
function StaleNote({
  citation,
  run,
  verdict,
  events,
}: {
  citation: Citation;
  run: RunDetail["run"];
  verdict: Verdict;
  events: RunEvent[];
}) {
  const change = record(verdict.evidence["change"]);
  if (!change) return null;
  const ts = str(change["ts"]);
  const operation = str(change["operation"]) ?? "modified";
  const sourceEventId = str(change["source_event_id"]);
  const sourceEvent = sourceEventId ? events.find((e) => e.id === sourceEventId) : undefined;
  const offset = ts ? formatOffset(Date.parse(ts) - Date.parse(run.created_at)) : null;
  const hashAtRead = str(verdict.evidence["hash_at_read"]);
  const hashAfter = str(verdict.evidence["hash_after"]);
  const matches = verdict.evidence["hash_at_read_matches_recorded"] === true;
  return (
    <p className="border-b border-line px-3 py-2 text-sm text-muted">
      {offset ? `Changed at ${offset} by ${operation}` : `Changed by ${operation}`}
      {sourceEvent ? ` #${sourceEvent.sequence}` : ""}.{" "}
      {matches ? (
        <>
          Hash at read {hashText(hashAtRead)} became {hashText(hashAfter)}.
        </>
      ) : (
        <>
          Hash of the retained read {hashText(hashAtRead)}; File Evidence recorded no matching hash
          for this file at read time.
        </>
      )}{" "}
      <Link
        to={`/runs/${run.id}?tab=files&path=${enc(citation.path ?? "")}`}
        className="text-accent underline"
      >
        Compare in file evidence
      </Link>
    </p>
  );
}

/** The `quote_not_found` body: the recorded lines (the read window that covers the claimed
 * range, `content.cited` here is the citation's claimed range) and the agent's quote, both
 * always open. */
function QuoteNotFoundBody({ citation }: { citation: Citation }) {
  const query = useCitationContent(citation.id, { before: 3, after: 3 }, true);
  return (
    <div className="flex flex-col">
      <div className="border-b border-line">
        <ContentState query={query}>
          {(content) => (
            <>
              <p className="border-b border-line bg-sunken px-3 py-1.5 text-xs font-medium text-muted">
                {content.window
                  ? `Recorded lines L${content.window[0]}-L${content.window[1]}`
                  : "Recorded lines"}
              </p>
              <LinesTable
                lines={content.lines}
                markRange={content.cited}
                marker="claimed"
                path={content.path}
              />
              <ContentFooter content={content} />
            </>
          )}
        </ContentState>
      </div>
      <AgentBlock citation={citation} />
    </div>
  );
}

function NotCheckableBody({ citation, verdict }: { citation: Citation; verdict: Verdict }) {
  const reasonCode = str(verdict.evidence["not_checkable_reason"]);
  const message =
    (isNotCheckableReason(reasonCode) ? NOT_CHECKABLE[reasonCode].sentence : null) ||
    verdict.reason;
  const readInfo = record(verdict.evidence["read"]);
  const blobRef = readInfo ? str(readInfo["blob_ref"]) : null;
  const provenance = readProvenance(blobRef);
  return (
    <div className="flex flex-col">
      <AgentBlock citation={citation} />
      <p className="px-3 pb-3 text-sm text-ink-soft [overflow-wrap:anywhere]">{message}</p>
      {provenance ? (
        <div className="flex flex-wrap gap-y-1 px-3 pb-3 font-mono text-xs">
          <ProvenanceLabel provenance={provenance} />
        </div>
      ) : null}
    </div>
  );
}

/** The collapsed "Show verdict evidence" disclosure; the only thing this component ever
 * collapses (the failure states above are always open). */
function EvidenceDisclosure({ citation }: { citation: Citation }) {
  const verdict = citation.verdict;
  if (!verdict) return null;
  return (
    <details className="border-t border-line px-3 py-2 text-sm">
      <summary className="cursor-pointer text-muted select-none">
        {isSeeded(verdict) ? "Seeded demo stand-in" : "Show verdict evidence"}
      </summary>
      <div className="mt-2 flex flex-col gap-2">
        <JsonInspector value={verdict.evidence} />
        {citation.checks > 1 ? (
          <p className="text-xs text-muted">{citation.checks} checks</p>
        ) : null}
      </div>
    </details>
  );
}

/**
 * `CitationView({ citation, run, events })`. `run` (run-relative times, the terminal check,
 * links to the file drawer and timeline) and `events` (the in-memory event feed, used to
 * resolve a stale write's own timeline sequence) are accepted by every verdict body.
 */
export function CitationView(props: {
  citation: Citation;
  run: RunDetail["run"];
  events: RunEvent[];
}) {
  const { citation, run, events } = props;
  const verdict = citation.verdict;
  const value = verdict?.verdict;
  const isFile = citation.kind === "file";
  const event = citation.event_id ? events.find((e) => e.id === citation.event_id) : undefined;

  let body: ReactNode;
  if (isFile && value === "quote_matches") {
    body = <RetainedPanel citation={citation} />;
  } else if (isFile && value === "stale" && verdict) {
    body = (
      <>
        <StaleNote citation={citation} run={run} verdict={verdict} events={events} />
        <RetainedPanel citation={citation} stale />
      </>
    );
  } else if (isFile && value === "quote_not_found") {
    body = <QuoteNotFoundBody citation={citation} />;
  } else if (
    !isFile &&
    event &&
    verdict &&
    (value === "quote_matches" || value === "quote_not_found")
  ) {
    body = <EventBody citation={citation} event={event} verdict={verdict} />;
  } else if (value === "never_read") {
    const subject = citation.path ?? citation.event_ref ?? "the cited event";
    body = (
      <>
        <AgentBlock citation={citation} />
        <p className="px-3 pb-3 text-sm text-ink-soft [overflow-wrap:anywhere]">
          This run has no record of reading {subject}.
        </p>
      </>
    );
  } else if (value === "read_not_observed" && verdict) {
    const windowInfo = record(verdict.evidence["window"]);
    const rawStatement = (windowInfo ? str(windowInfo["statement"]) : null) ?? verdict.reason ?? "";
    const statement = rawStatement.endsWith(".") ? rawStatement.slice(0, -1) : rawStatement;
    const subject = citation.path ?? citation.event_ref ?? "the cited event";
    body = (
      <>
        <AgentBlock citation={citation} />
        <p className="px-3 pb-3 text-sm text-ink-soft [overflow-wrap:anywhere]">
          {statement}. AgentTape cannot tell whether {subject} was read during that gap.
        </p>
      </>
    );
  } else if (value === "not_checkable" && verdict) {
    body = <NotCheckableBody citation={citation} verdict={verdict} />;
  } else {
    // No verdict yet, or an event citation whose verdict this component does not (yet)
    // build a dedicated body for: show the agent's own quote until one is decided.
    body = <AgentBlock citation={citation} />;
  }

  return (
    <section
      aria-label={`Citation: ${citation.path ?? citation.event_ref ?? "citation"}`}
      className="overflow-hidden rounded-[var(--radius-panel)] border border-line"
    >
      {isFile ? (
        <Header citation={citation} run={run} />
      ) : (
        <EventHeader citation={citation} run={run} event={event} />
      )}
      {!isFile ? <ReproductionLine citation={citation} run={run} /> : null}
      {body}
      <EvidenceDisclosure citation={citation} />
    </section>
  );
}
