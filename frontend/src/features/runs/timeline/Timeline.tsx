import { ChevronRight, Search } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router";

import type { RunDetail, RunEvent } from "@/api/types";
import { Badge, CodeBlock, EmptyState, JsonInspector, Skeleton } from "@/components/ui";
import { cn } from "@/lib/cn";
import { formatBytes, formatOffset, humanize, text } from "@/lib/format";

import {
  countByGroup,
  EMPTY_FILTERS,
  EVENT_GROUPS,
  filterEvents,
  groupOf,
  type MinSeverity,
  type TimelineFilters,
} from "./model";

const MAX_RENDERED = 1500;

/** The last `MAX_RENDERED` events or, when `?event=<sequence>` names one further back, a
 * same-sized window that still includes it, so a deep link never lands on a row this
 * component chose not to render. */
function windowIncluding(filtered: RunEvent[], targetSequence: number | null): RunEvent[] {
  if (filtered.length <= MAX_RENDERED) return filtered;
  const idx =
    targetSequence === null ? -1 : filtered.findIndex((e) => e.sequence === targetSequence);
  if (idx === -1) return filtered.slice(-MAX_RENDERED);
  let start = Math.max(0, idx - Math.floor(MAX_RENDERED / 2));
  if (start + MAX_RENDERED > filtered.length) start = filtered.length - MAX_RENDERED;
  return filtered.slice(start, start + MAX_RENDERED);
}

/** Marker colour per event group; severity overrides for warnings and errors. */
const GROUP_COLOR: Record<string, string> = {
  agent: "var(--accent)",
  tools: "var(--st-waiting)",
  commands: "var(--st-preparing)",
  output: "var(--faint)",
  files: "var(--st-succeeded)",
  lifecycle: "var(--ink-soft)",
  approvals: "var(--st-waiting)",
  evaluations: "var(--st-succeeded)",
  usage: "var(--muted)",
  problems: "var(--st-cancelling)",
};

function markerColor(event: RunEvent): string {
  if (event.severity === "error" || event.severity === "critical") return "var(--st-failed)";
  if (event.severity === "warning") return "var(--st-cancelling)";
  return GROUP_COLOR[groupOf(event.type)] ?? "var(--muted)";
}

function str(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function EventBody({ event }: { event: RunEvent }) {
  const p = event.payload;
  switch (event.type) {
    case "agent_message":
      return (
        <p className="max-w-[80ch] whitespace-pre-wrap text-ink">{str(p.text) ?? event.summary}</p>
      );
    case "reasoning_summary":
      return (
        <div className="flex flex-col gap-1">
          <p className="max-w-[80ch] whitespace-pre-wrap text-ink">
            {str(p.text) ?? event.summary}
          </p>
          <p className="text-xs text-muted">
            A summary the adapter chose to report. Hidden reasoning is never recorded.
          </p>
        </div>
      );
    case "tool_call":
      return (
        <JsonInspector
          label={`Input to ${str(p.tool) ?? "tool"}`}
          value={p.input ?? {}}
          defaultDepth={2}
        />
      );
    case "tool_result":
      return (
        <CodeBlock
          label={p.is_error ? "Tool error" : "Tool output"}
          code={str(p.output) ?? ""}
          maxHeight="16rem"
        />
      );
    case "command": {
      const argv = Array.isArray(p.argv) ? p.argv.map(String) : [];
      return (
        <div className="flex flex-col gap-2">
          <CodeBlock
            label="Command (argument array, no shell)"
            code={JSON.stringify(argv)}
            maxHeight="8rem"
          />
          <p className="text-xs text-muted">
            {str(p.cwd) ? (
              <>
                Working directory <code>{str(p.cwd)}</code>.{" "}
              </>
            ) : null}
            {typeof p.pid === "number" ? <>Process {p.pid}.</> : null}
          </p>
        </div>
      );
    }
    case "stdout":
    case "stderr":
      return (
        <CodeBlock
          label={event.type === "stdout" ? "Standard output" : "Standard error"}
          code={str(p.text) ?? ""}
          maxHeight="20rem"
        />
      );
    case "usage":
      return (
        <p className="text-sm">
          {text(p.input_tokens)} input tokens, {text(p.output_tokens)} output tokens{" "}
          {p.simulated ? <Badge tone="warning">Simulated by the Demo Agent</Badge> : null}
        </p>
      );
    case "git_change": {
      const files = Array.isArray(p.files) ? (p.files as Record<string, unknown>[]) : [];
      return (
        <ul className="flex flex-col gap-0.5 text-sm">
          {files.map((f) => (
            <li key={String(f.path)} className="flex gap-2">
              <span className="w-16 text-muted">{humanize(String(f.change_type))}</span>
              <code>{String(f.path)}</code>
              <span className="text-[var(--st-succeeded)]">+{text(f.insertions)}</span>
              <span className="text-[var(--st-failed)]">-{text(f.deletions)}</span>
            </li>
          ))}
        </ul>
      );
    }
    case "artifact":
      return (
        <p className="text-sm">
          <code>{str(p.name)}</code>
          {typeof p.size_bytes === "number" ? `, ${formatBytes(p.size_bytes)}` : ""}. Open it from
          the Artifacts tab.
        </p>
      );
    case "evaluation":
      return (
        <div className="flex flex-col gap-1 text-sm">
          <p>{str(p.explanation)}</p>
          <JsonInspector label="Evidence" value={p.evidence ?? {}} />
        </div>
      );
    default:
      return Object.keys(p).length ? <JsonInspector value={p} /> : null;
  }
}

function EventRow({
  event,
  runStart,
  expanded,
  targeted,
  onToggle,
}: {
  event: RunEvent;
  runStart: number;
  expanded: boolean;
  targeted: boolean;
  onToggle: () => void;
}) {
  const [raw, setRaw] = useState(false);
  const offset = new Date(event.ts).getTime() - runStart;
  const problem = event.severity === "error" || event.severity === "critical";
  return (
    <li
      id={`event-${event.sequence}`}
      className={cn(
        "group relative grid grid-cols-[4.75rem_1.25rem_minmax(0,1fr)] gap-x-2",
        targeted && "bg-accent-soft ring-1 ring-inset ring-accent",
      )}
      data-event-type={event.type}
      data-sequence={event.sequence}
    >
      <span
        className="pt-2 text-right font-mono text-xs text-faint tabular-nums"
        title={`Sequence ${event.sequence}, ${event.ts}`}
      >
        {formatOffset(offset)}
      </span>
      <span className="relative flex justify-center">
        <span aria-hidden className="absolute inset-y-0 w-px bg-line" />
        <span
          aria-hidden
          className={cn(
            "relative mt-[0.6rem] size-2.5 rounded-full ring-4 ring-[var(--surface)]",
            event.type === "lifecycle" && "rounded-[2px]",
          )}
          style={{ backgroundColor: markerColor(event) }}
        />
      </span>
      <div className="min-w-0 pb-1">
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={expanded}
          className={cn(
            "flex w-full items-start gap-2 rounded-[var(--radius-control)] px-2 py-1.5 text-left hover:bg-sunken",
            problem && "text-[var(--st-failed)]",
          )}
        >
          <ChevronRight
            aria-hidden
            className={cn(
              "mt-1 size-3.5 shrink-0 text-faint transition-transform",
              expanded && "rotate-90",
            )}
          />
          <span
            className={cn(
              "min-w-0 flex-1 break-words",
              event.type === "stdout" || event.type === "stderr"
                ? "font-mono text-[0.8125rem] text-ink-soft"
                : "",
            )}
          >
            {event.summary}
          </span>
          <span className="hidden shrink-0 text-xs text-muted sm:inline">
            {humanize(event.type)}
          </span>
        </button>
        {expanded ? (
          <div className="ml-7 flex flex-col gap-2 pt-1 pb-3">
            <EventBody event={event} />
            {event.redaction?.redacted ? (
              <p className="text-xs text-muted">
                {event.redaction.count} value{event.redaction.count === 1 ? " was" : "s were"}{" "}
                redacted before storage ({event.redaction.kinds.join(", ")}).
              </p>
            ) : null}
            <div>
              <button
                type="button"
                onClick={() => setRaw(!raw)}
                aria-expanded={raw}
                className="text-xs text-accent hover:underline"
              >
                {raw ? "Hide raw event" : "Show raw event"}
              </button>
              {raw ? (
                <div className="mt-2">
                  <JsonInspector label={`Event ${event.sequence}`} value={event} defaultDepth={1} />
                </div>
              ) : null}
            </div>
          </div>
        ) : null}
      </div>
    </li>
  );
}

export function Timeline({
  detail,
  events,
  loading,
  live,
}: {
  detail: RunDetail;
  events: RunEvent[];
  loading: boolean;
  live: boolean;
}) {
  const [params, setParams] = useSearchParams();
  const eventParam = params.get("event");
  const targetSequence =
    eventParam !== null && /^\d+$/.test(eventParam) ? Number(eventParam) : null;
  const targetExists = targetSequence !== null && events.some((e) => e.sequence === targetSequence);

  const [filters, setFilters] = useState<TimelineFilters>(EMPTY_FILTERS);
  const [expanded, setExpanded] = useState<Set<number>>(() => new Set());
  const [follow, setFollow] = useState(true);
  const endRef = useRef<HTMLDivElement>(null);
  const scrolledToRef = useRef<number | null>(null);

  // `?event=<sequence>` (from a stale-citation "Compare in file evidence" link, or a Findings
  // citation's "read #N" link) always lands on that event: these are derived from the raw
  // filter/expanded/follow state rather than synced onto it, so a target that arrives after
  // mount (the events feed loading in) or a target that a filter hides is still honoured on
  // every render, with no effect needed to keep state in sync.
  const filtered = useMemo(() => {
    const base = filterEvents(events, filters);
    if (targetExists && !base.some((e) => e.sequence === targetSequence)) {
      return filterEvents(events, EMPTY_FILTERS);
    }
    return base;
  }, [events, filters, targetExists, targetSequence]);
  const effectiveExpanded = useMemo(() => {
    if (!targetExists || expanded.has(targetSequence)) return expanded;
    return new Set(expanded).add(targetSequence);
  }, [expanded, targetExists, targetSequence]);
  const effectiveFollow = targetExists ? false : follow;

  // Following live would scroll away from a linked event, so it is off while one is shown.
  // Turning it back on is taken at its word: leave the linked event (drop `?event=`, keeping
  // every other param) and follow new events, instead of snapping the box back unchecked.
  function changeFollow(next: boolean) {
    setFollow(next);
    if (next && targetExists) {
      setParams(
        (prev) => {
          const kept = new URLSearchParams(prev);
          kept.delete("event");
          return kept;
        },
        { replace: true },
      );
    }
  }

  const counts = useMemo(() => countByGroup(events), [events]);
  const visible = useMemo(
    () => windowIncluding(filtered, targetSequence),
    [filtered, targetSequence],
  );
  const runStart = new Date(detail.run.created_at).getTime();
  const nodeKeys = useMemo(
    () => [...new Set(events.map((e) => e.node_key).filter((k): k is string => Boolean(k)))],
    [events],
  );

  useEffect(() => {
    if (live && effectiveFollow) endRef.current?.scrollIntoView({ block: "nearest" });
  }, [visible.length, live, effectiveFollow]);

  // Scrolling and focus are DOM side effects, not React state, so they stay in an effect:
  // once the targeted row exists in the DOM, bring it into view and focus its toggle, once.
  useEffect(() => {
    if (targetSequence === null || scrolledToRef.current === targetSequence) return;
    const row = document.getElementById(`event-${targetSequence}`);
    if (!row) return;
    row.scrollIntoView({ block: "center" });
    row.querySelector<HTMLButtonElement>("button")?.focus();
    scrolledToRef.current = targetSequence;
  }, [targetSequence, visible]);

  const toggleGroup = (id: string) =>
    setFilters((f) => ({
      ...f,
      groups: f.groups.includes(id) ? f.groups.filter((g) => g !== id) : [...f.groups, id],
    }));

  const toggle = (seq: number) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(seq)) next.delete(seq);
      else next.add(seq);
      return next;
    });

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-3" role="group" aria-label="Timeline filters">
        <div className="flex flex-wrap gap-1.5">
          {EVENT_GROUPS.filter((g) => (counts[g.id] ?? 0) > 0 || filters.groups.includes(g.id)).map(
            (g) => {
              const on = filters.groups.includes(g.id);
              return (
                <button
                  key={g.id}
                  type="button"
                  aria-pressed={on}
                  onClick={() => toggleGroup(g.id)}
                  className={cn(
                    "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-sm transition-colors",
                    on
                      ? "border-accent bg-accent-soft text-ink"
                      : "border-line bg-surface text-ink-soft hover:border-line-strong",
                  )}
                >
                  <span
                    aria-hidden
                    className="size-2 rounded-full"
                    style={{ backgroundColor: GROUP_COLOR[g.id] }}
                  />
                  {g.label}
                  <span className="text-xs text-muted tabular-nums">{counts[g.id] ?? 0}</span>
                </button>
              );
            },
          )}
        </div>
        <div className="flex flex-wrap items-end gap-2">
          <label className="relative flex min-w-[14rem] flex-1 items-center">
            <span className="sr-only">Search events</span>
            <Search aria-hidden className="absolute left-2.5 size-4 text-faint" />
            <input
              type="search"
              value={filters.text}
              onChange={(e) => setFilters((f) => ({ ...f, text: e.target.value }))}
              placeholder="Search summaries and output"
              className="h-8 w-full rounded-[var(--radius-control)] border border-line-strong bg-surface pr-2 pl-8 text-sm text-ink"
            />
          </label>
          <label className="flex items-center gap-1.5 text-sm text-muted">
            Severity
            <select
              value={filters.minSeverity}
              onChange={(e) =>
                setFilters((f) => ({ ...f, minSeverity: e.target.value as MinSeverity }))
              }
              className="h-8 rounded-[var(--radius-control)] border border-line-strong bg-surface px-2 text-sm text-ink"
            >
              <option value="all">All</option>
              <option value="notice">Notice and above</option>
              <option value="warning">Warnings and errors</option>
              <option value="error">Errors only</option>
            </select>
          </label>
          {detail.attempts.length > 1 ? (
            <label className="flex items-center gap-1.5 text-sm text-muted">
              Attempt
              <select
                value={filters.attemptId ?? ""}
                onChange={(e) => setFilters((f) => ({ ...f, attemptId: e.target.value || null }))}
                className="h-8 rounded-[var(--radius-control)] border border-line-strong bg-surface px-2 text-sm text-ink"
              >
                <option value="">All attempts</option>
                {detail.attempts.map((a) => (
                  <option key={a.id} value={a.id}>
                    Attempt {a.attempt_number}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
          {nodeKeys.length > 0 ? (
            <label className="flex items-center gap-1.5 text-sm text-muted">
              Step
              <select
                value={filters.nodeKey ?? ""}
                onChange={(e) => setFilters((f) => ({ ...f, nodeKey: e.target.value || null }))}
                className="h-8 rounded-[var(--radius-control)] border border-line-strong bg-surface px-2 text-sm text-ink"
              >
                <option value="">All steps</option>
                {nodeKeys.map((k) => (
                  <option key={k} value={k}>
                    {k}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
          {live ? (
            <label className="flex h-8 items-center gap-1.5 text-sm text-muted">
              <input
                type="checkbox"
                checked={effectiveFollow}
                onChange={(e) => changeFollow(e.target.checked)}
                title={
                  targetExists
                    ? "Paused on the linked event. Turn on to leave it and follow new events."
                    : undefined
                }
                className="accent-[var(--accent)]"
              />
              Follow live
            </label>
          ) : null}
        </div>
        <p className="text-sm text-muted" aria-live="polite">
          Showing {filtered.length} of {events.length} events
          {filtered.length > MAX_RENDERED ? ` (latest ${MAX_RENDERED} rendered)` : ""}
          {live ? "; new events stream in live" : ""}
        </p>
      </div>

      {loading && events.length === 0 ? (
        <Skeleton lines={6} />
      ) : events.length === 0 ? (
        <EmptyState
          title="No events yet"
          description="Events appear here as soon as a worker picks up the run."
        />
      ) : filtered.length === 0 ? (
        <EmptyState
          title="No events match these filters"
          action={
            <button
              type="button"
              className="text-accent hover:underline"
              onClick={() => setFilters(EMPTY_FILTERS)}
            >
              Clear filters
            </button>
          }
        />
      ) : (
        <ol aria-label="Run timeline" className="flex flex-col">
          {visible.map((event) => (
            <EventRow
              key={event.sequence}
              event={event}
              runStart={runStart}
              expanded={effectiveExpanded.has(event.sequence)}
              targeted={event.sequence === targetSequence}
              onToggle={() => toggle(event.sequence)}
            />
          ))}
          {live ? (
            <li className="grid grid-cols-[4.75rem_1.25rem_minmax(0,1fr)] gap-x-2" aria-hidden>
              <span />
              <span className="flex justify-center">
                <span className="at-live-dot mt-2 size-2.5 rounded-full bg-live" />
              </span>
              <span className="px-2 py-1.5 text-sm text-live">Recording</span>
            </li>
          ) : null}
        </ol>
      )}
      <div ref={endRef} />
    </div>
  );
}
