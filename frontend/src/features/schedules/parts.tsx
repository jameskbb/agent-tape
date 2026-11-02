import { AlertTriangle, CircleCheck, CirclePause } from "lucide-react";
import { cloneElement, isValidElement, useId, type ReactElement, type ReactNode } from "react";

import { describeError } from "@/api/client";
import { Badge, RelativeTime, Skeleton } from "@/components/ui";
import { cn } from "@/lib/cn";

import { useSchedulerStatus, type SchedulePreview } from "./api";
import { OCCURRENCE_LABELS, splitLocalIso } from "./timing";

export const inputClass =
  "h-9 min-w-0 rounded-[var(--radius-control)] border border-line-strong bg-surface px-2.5 text-base text-ink disabled:opacity-60";
export const textareaClass =
  "w-full rounded-[var(--radius-control)] border border-line-strong bg-surface px-2.5 py-2 text-base text-ink";

/**
 * A labelled form field. The label is always tied to the control: pass ``htmlFor`` for a
 * control with its own id, otherwise a single child element receives a generated id (and
 * the hint or error as its description).
 */
export function Field({
  label,
  hint,
  error,
  htmlFor,
  children,
  className,
}: {
  label: string;
  hint?: ReactNode;
  error?: string | null;
  htmlFor?: string;
  children: ReactNode;
  className?: string;
}) {
  const generated = useId();
  const noteId = `${generated}-note`;
  const labelId = `${generated}-label`;
  const describedBy = error || hint ? noteId : undefined;
  const isControl =
    isValidElement(children) &&
    typeof children.type === "string" &&
    ["input", "select", "textarea"].includes(children.type);
  let controlId = htmlFor;
  let control: ReactNode = children;
  if (!controlId && isControl) {
    const el = children as ReactElement<{ id?: string; "aria-describedby"?: string }>;
    controlId = el.props.id ?? generated;
    control = cloneElement(el, {
      id: controlId,
      "aria-describedby": describedBy ?? el.props["aria-describedby"],
    });
  } else if (!controlId) {
    // A composite control (e.g. amount + unit): label the group instead.
    control = (
      <div role="group" aria-labelledby={labelId} aria-describedby={describedBy}>
        {children}
      </div>
    );
  }
  return (
    <div className={cn("flex min-w-0 flex-col gap-1", className)}>
      {controlId ? (
        <label id={labelId} htmlFor={controlId} className="text-sm font-medium text-ink-soft">
          {label}
        </label>
      ) : (
        <span id={labelId} className="text-sm font-medium text-ink-soft">
          {label}
        </span>
      )}
      {control}
      {error ? (
        <p id={noteId} className="text-sm text-[var(--st-failed)]" role="alert">
          {error}
        </p>
      ) : hint ? (
        <p id={noteId} className="text-sm text-muted">
          {hint}
        </p>
      ) : null}
    </div>
  );
}

export function OccurrenceBadge({ status }: { status: string }) {
  const meta = OCCURRENCE_LABELS[status] ?? { label: status, tone: "neutral" };
  return (
    <Badge tone={meta.tone as "neutral" | "success" | "warning" | "danger" | "violet" | "accent"}>
      {meta.label}
    </Badge>
  );
}

/**
 * Upcoming occurrences as a reel: one row per run with the weekday and date on the left,
 * the local wall-clock time in large tabular figures, and the UTC offset. DST adjustments
 * are called out on the row they affect.
 */
export function NextRunsReel({
  preview,
  loading,
  error,
  emptyHint,
}: {
  preview: SchedulePreview | undefined;
  loading?: boolean;
  error?: unknown;
  emptyHint?: string;
}) {
  if (error) {
    return <p className="text-sm text-[var(--st-failed)]">{describeError(error)}</p>;
  }
  if (!preview) {
    return loading ? <Skeleton className="h-40" /> : null;
  }
  if (!preview.valid) {
    return (
      <p className="flex items-start gap-2 text-sm text-[var(--st-failed)]" role="status">
        <AlertTriangle aria-hidden className="mt-0.5 size-4 shrink-0" />
        {preview.error}
      </p>
    );
  }
  if (preview.occurrences.length === 0) {
    return (
      <p className="text-sm text-muted" role="status">
        {emptyHint ??
          "No upcoming runs: the time is in the past or outside the start and end window."}
      </p>
    );
  }
  const dates = preview.occurrences.map((o) => splitLocalIso(o.local)?.date ?? o.local);
  return (
    <div role="status" aria-live="polite" aria-busy={loading || undefined}>
      <ol
        aria-label={`Next runs in ${preview.timezone}`}
        className={cn(
          "relative flex flex-col border-l-2 border-accent pl-4 transition-opacity",
          loading && "opacity-60",
        )}
      >
        {preview.occurrences.map((o, i) => {
          const p = splitLocalIso(o.local);
          const newDay = i === 0 || dates[i] !== dates[i - 1];
          return (
            <li key={o.at} className="relative py-1.5">
              <span
                aria-hidden
                className={cn(
                  "absolute top-1/2 -left-[1.36rem] size-2.5 -translate-y-1/2 rounded-full border-2 border-accent",
                  i === 0 ? "bg-accent" : "bg-surface",
                )}
              />
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
                <span
                  className={cn(
                    "w-[7.5rem] shrink-0 text-sm",
                    newDay ? "text-ink-soft" : "text-faint",
                  )}
                >
                  {p ? `${p.weekday} ${p.day} ${p.month} ${p.year}` : o.local}
                </span>
                <span className="font-mono text-lg font-medium text-ink tabular-nums">
                  {p?.time ?? ""}
                </span>
                <span className="text-xs text-muted">{o.utc_offset}</span>
                {i === 0 ? <Badge tone="accent">Next</Badge> : null}
              </div>
              {o.note ? (
                <p className="mt-0.5 text-sm text-[var(--st-cancelling)]">{o.note}</p>
              ) : null}
            </li>
          );
        })}
      </ol>
      {preview.finished ? (
        <p className="mt-2 text-sm text-muted">No further runs after these.</p>
      ) : null}
    </div>
  );
}

export function SchedulerStatusNote() {
  const status = useSchedulerStatus();
  if (!status.data) return null;
  const s = status.data;
  const running = s.state === "running";
  return (
    <div
      className={cn(
        "flex flex-wrap items-center gap-2 rounded-[var(--radius-panel)] border px-3 py-2 text-sm",
        running
          ? "border-line bg-surface text-ink-soft"
          : "border-[color-mix(in_srgb,var(--st-cancelling)_45%,transparent)] bg-[var(--st-cancelling-bg)] text-ink",
      )}
      role="status"
    >
      {running ? (
        <CircleCheck aria-hidden className="size-4 text-[var(--st-succeeded)]" />
      ) : (
        <CirclePause aria-hidden className="size-4 text-[var(--st-cancelling)]" />
      )}
      <span className="font-medium">
        {running
          ? "Scheduler running"
          : s.state === "standby"
            ? "Scheduler starting"
            : "Scheduler not running"}
      </span>
      <span className="text-muted">
        {running ? "Due runs are created automatically." : s.message}
      </span>
      {s.last_tick_at ? (
        <span className="text-muted">
          Last checked <RelativeTime iso={s.last_tick_at} />
        </span>
      ) : null}
    </div>
  );
}
