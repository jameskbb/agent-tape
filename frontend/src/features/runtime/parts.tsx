/** Shared bits for the runtime views: support badges and lifecycle tones. */
import { Badge } from "@/components/ui";

import type { RuntimeCapability } from "./api";

export const SUPPORT_TONE = {
  supported: "success",
  degraded: "warning",
  unsupported: "neutral",
} as const;

export const SUPPORT_LABEL = {
  supported: "Supported",
  degraded: "Degraded",
  unsupported: "Not supported",
} as const;

export function SupportBadge({ cap }: { cap: RuntimeCapability }) {
  return (
    <Badge tone={SUPPORT_TONE[cap.support]} title={cap.note}>
      {SUPPORT_LABEL[cap.support]}
    </Badge>
  );
}

const LIFECYCLE_TONE: Record<string, "success" | "warning" | "live" | "neutral" | "violet"> = {
  working: "live",
  blocked: "warning",
  idle: "success",
  done: "success",
  starting: "neutral",
  unknown: "neutral",
};

export function LifecycleBadge({ state }: { state: string | null }) {
  if (!state) return <Badge>Not observed</Badge>;
  return <Badge tone={LIFECYCLE_TONE[state] ?? "neutral"}>{state}</Badge>;
}

export function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid gap-1 py-2 sm:grid-cols-[12rem_minmax(0,1fr)] sm:gap-4">
      <dt className="text-sm text-muted">{label}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </div>
  );
}
