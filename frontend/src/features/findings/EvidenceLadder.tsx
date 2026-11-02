/**
 * The six-rung evidence ladder: fixed slots, weakest to strongest, always in this
 * order, each a focusable button that explains itself (there is no Tooltip primitive, so a
 * pressed rung's one-line explanation is what a tooltip would have said). Colour is
 * reinforcement only: every rung carries its own icon and phrase, and an unachieved rung
 * always renders outlined (never a bare icon, never the live/orange tone this component
 * does not use at all: the Challenge rung is model judgment, never a status to watch).
 */
import {
  Check,
  MessageSquareQuote,
  Repeat,
  Scale,
  SquareTerminal,
  UserCheck,
  type LucideIcon,
} from "lucide-react";
import { useId, useState } from "react";
import { Link } from "react-router";

import { cn } from "@/lib/cn";
import { useNow } from "@/lib/useNow";

import type { Challenge, Finding } from "./api";
import { ladder, type Rung, type RungIcon, type RungTone } from "./model";

const ICONS: Record<RungIcon, LucideIcon> = {
  MessageSquareQuote,
  Check,
  SquareTerminal,
  Repeat,
  Scale,
  UserCheck,
};

/** An unachieved rung is always `outline`, regardless of its semantic tone: the weakest
 * rendering the ladder has, reserved for "this has not happened (yet, or at all)". */
const TONE_CLASS: Record<RungTone | "outline", string> = {
  neutral: "border-transparent bg-sunken text-ink-soft",
  success: "border-transparent bg-[var(--st-succeeded-bg)] text-[var(--st-succeeded)]",
  violet: "border-transparent bg-[var(--st-waiting-bg)] text-[var(--st-waiting)]",
  accent: "border-transparent bg-accent-soft text-accent",
  outline: "border-line text-faint bg-transparent",
};

export function EvidenceLadder({
  finding,
  challenges,
}: {
  finding: Finding;
  challenges: readonly Challenge[];
}) {
  // Rung 6's relative-time phrase keeps advancing while the ladder stays on screen.
  const now = useNow(30_000, true);
  const rungs = ladder(finding, challenges, now);
  const [activeSlot, setActiveSlot] = useState<Rung["slot"] | null>(null);
  const baseId = useId();
  const active = rungs.find((rung) => rung.slot === activeSlot) ?? null;

  return (
    <div className="flex flex-col gap-2">
      <ul aria-label="Evidence for this finding" className="flex flex-wrap items-center gap-2">
        {rungs.map((rung) => {
          const Icon = ICONS[rung.icon];
          const expanded = activeSlot === rung.slot;
          const panelId = `${baseId}-${rung.slot}`;
          const tone = rung.achieved ? rung.tone : "outline";
          return (
            <li key={rung.slot} className="flex items-center gap-1.5">
              <button
                type="button"
                aria-expanded={expanded}
                aria-controls={panelId}
                onClick={() => setActiveSlot(expanded ? null : rung.slot)}
                data-tone={tone}
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-[4px] border px-2 py-1 text-xs font-medium whitespace-nowrap transition-colors",
                  TONE_CLASS[tone],
                )}
              >
                <Icon aria-hidden className="size-3.5" />
                {rung.label}
              </button>
              {rung.href ? (
                <Link to={rung.href} className="text-xs text-accent hover:underline">
                  Open the challenge run
                </Link>
              ) : null}
            </li>
          );
        })}
      </ul>
      {active ? (
        <p
          id={`${baseId}-${active.slot}`}
          role="region"
          aria-label={`About ${active.name}`}
          className="text-sm text-muted"
        >
          {active.explanation}
        </p>
      ) : null}
    </div>
  );
}
