import type { ReactNode } from "react";

import { cn } from "@/lib/cn";

type Tone = "neutral" | "accent" | "live" | "success" | "danger" | "warning" | "violet";

const tones: Record<Tone, string> = {
  neutral: "bg-sunken text-ink-soft border-line",
  accent: "bg-accent-soft text-accent border-transparent",
  live: "bg-live-soft text-live border-transparent",
  success: "bg-[var(--st-succeeded-bg)] text-[var(--st-succeeded)] border-transparent",
  danger: "bg-[var(--st-failed-bg)] text-[var(--st-failed)] border-transparent",
  warning: "bg-[var(--st-cancelling-bg)] text-[var(--st-cancelling)] border-transparent",
  violet: "bg-[var(--st-waiting-bg)] text-[var(--st-waiting)] border-transparent",
};

export function Badge({
  tone = "neutral",
  children,
  className,
  title,
}: {
  tone?: Tone;
  children: ReactNode;
  className?: string;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex items-center gap-1 rounded-[4px] border px-1.5 py-px text-xs font-medium whitespace-nowrap",
        tones[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}
