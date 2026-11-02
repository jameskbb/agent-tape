import {
  Ban,
  Check,
  CircleDashed,
  Clock3,
  Hourglass,
  Loader,
  OctagonX,
  PauseCircle,
  SkipForward,
  TimerOff,
} from "lucide-react";
import type { ComponentType } from "react";

import type { RunStatus } from "@/api/types";
import { cn } from "@/lib/cn";

interface StatusStyle {
  label: string;
  var: string;
  Icon: ComponentType<{ className?: string; "aria-hidden"?: boolean }>;
  live?: boolean;
}

export const STATUS_STYLES: Record<RunStatus, StatusStyle> = {
  queued: { label: "Queued", var: "queued", Icon: Clock3 },
  preparing: { label: "Preparing", var: "preparing", Icon: Loader, live: true },
  running: { label: "Running", var: "running", Icon: CircleDashed, live: true },
  waiting_for_approval: { label: "Waiting for approval", var: "waiting", Icon: PauseCircle },
  cancelling: { label: "Cancelling", var: "cancelling", Icon: Hourglass, live: true },
  cancelled: { label: "Cancelled", var: "cancelled", Icon: Ban },
  succeeded: { label: "Succeeded", var: "succeeded", Icon: Check },
  failed: { label: "Failed", var: "failed", Icon: OctagonX },
  timed_out: { label: "Timed out", var: "timed-out", Icon: TimerOff },
  skipped: { label: "Skipped", var: "skipped", Icon: SkipForward },
};

export function statusLabel(status: RunStatus): string {
  return STATUS_STYLES[status].label;
}

/** Run/attempt/step status. Colour is never the only signal: each status has an icon and text. */
export function StatusPill({
  status,
  size = "md",
  className,
}: {
  status: RunStatus;
  size?: "sm" | "md";
  className?: string;
}) {
  const style = STATUS_STYLES[status] as StatusStyle | undefined;
  if (!style) {
    return <span className="text-sm text-muted">{status}</span>;
  }
  const { Icon } = style;
  return (
    <span
      data-status={status}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full font-medium whitespace-nowrap",
        size === "sm" ? "px-2 py-0.5 text-xs" : "px-2.5 py-0.5 text-sm",
        className,
      )}
      style={{ color: `var(--st-${style.var})`, backgroundColor: `var(--st-${style.var}-bg)` }}
    >
      {style.live ? (
        <span aria-hidden className="at-live-dot size-1.5 rounded-full bg-live" />
      ) : (
        <Icon aria-hidden className={size === "sm" ? "size-3" : "size-3.5"} />
      )}
      {style.label}
    </span>
  );
}
