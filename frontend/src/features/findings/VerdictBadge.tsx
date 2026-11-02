/** Icon plus phrase for a citation verdict; colour is reinforcement, never the only signal. */
import {
  Check,
  CircleMinus,
  CircleX,
  Clock,
  EyeOff,
  type LucideIcon,
  Quote,
  RefreshCw,
} from "lucide-react";

import { Badge } from "@/components/ui";

import { isSeeded, VERDICT_LABEL, type BadgeStatus } from "./model";

type Tone = "success" | "warning" | "danger" | "neutral";

/** Tone, icon, and phrase per status; every verdict's phrase comes from `VERDICT_LABEL`,
 * so only the three statuses that are not verdicts carry their own. */
const SPEC: Record<BadgeStatus, { tone: Tone; icon: LucideIcon; phrase: string }> = {
  quote_matches: { tone: "success", icon: Check, phrase: VERDICT_LABEL.quote_matches.badge },
  stale: { tone: "warning", icon: Clock, phrase: VERDICT_LABEL.stale.badge },
  quote_not_found: { tone: "danger", icon: CircleX, phrase: VERDICT_LABEL.quote_not_found.badge },
  never_read: { tone: "danger", icon: EyeOff, phrase: VERDICT_LABEL.never_read.badge },
  read_not_observed: {
    tone: "neutral",
    icon: EyeOff,
    phrase: VERDICT_LABEL.read_not_observed.badge,
  },
  not_checkable: { tone: "neutral", icon: CircleMinus, phrase: VERDICT_LABEL.not_checkable.badge },
  declared_only: { tone: "neutral", icon: Quote, phrase: VERDICT_LABEL.declared_only.badge },
  checking: { tone: "neutral", icon: RefreshCw, phrase: "Checking" },
  not_checked_yet: { tone: "neutral", icon: RefreshCw, phrase: "Not checked yet" },
  checked_when_finished: { tone: "neutral", icon: Clock, phrase: "Checked when the run finishes" },
};

/** The eight `not_checkable_reason` codes the backend accepts (`findings/verify.py`
 * `NOT_CHECKABLE_REASONS`, validated in `record_verdict`). */
export type NotCheckableReason =
  | "outside_boundary"
  | "no_quote"
  | "quote_truncated"
  | "no_content_recorded"
  | "content_not_line_numbered"
  | "size_cap_exceeded"
  | "redacted_at_cited_span"
  | "blob_unavailable";

/**
 * Per reason code: `short`, the closed-vocabulary label this badge appends after "Cannot
 * check" (the only text it ever appends inline), and `sentence`, the Copywriting Contract's
 * exact sentence, independent of the backend's own `reason` text (which is not written for
 * a reader, and can embed the cited path). `CitationView`'s body shows the sentence; this
 * badge only ever shows it in the `title`, never inline, so a long sentence can never clip.
 */
export const NOT_CHECKABLE: Record<NotCheckableReason, { short: string; sentence: string }> = {
  outside_boundary: {
    short: "outside project",
    sentence: "The cited path is outside the run's repository boundary, so no read was recorded.",
  },
  no_quote: {
    short: "no quote",
    sentence: "The citation gives a line range but no quote to check.",
  },
  quote_truncated: {
    short: "quote truncated",
    sentence:
      "The quote was cut at 4,000 characters before it was recorded, so it cannot be matched exactly.",
  },
  no_content_recorded: {
    short: "no content recorded",
    sentence: "The read was recorded without its content.",
  },
  content_not_line_numbered: {
    short: "no line numbers",
    sentence: "The recorded read has no line numbers, so a line range cannot be located.",
  },
  size_cap_exceeded: {
    short: "size cap",
    sentence:
      "The cited text falls past the size cap on what AgentTape recorded: 1 MiB of a read, 16 KiB of an event.",
  },
  redacted_at_cited_span: {
    short: "redacted",
    sentence:
      "Text that looks like a secret was redacted inside the cited lines, so the quote cannot be compared.",
  },
  blob_unavailable: {
    short: "unavailable",
    sentence: "The retained content is missing or could not be loaded.",
  },
};

/** True for one of the eight known codes; an untyped backend string (or an inherited object
 * key such as "toString") that is not one of them never selects copy. */
export function isNotCheckableReason(
  value: string | null | undefined,
): value is NotCheckableReason {
  return typeof value === "string" && Object.hasOwn(NOT_CHECKABLE, value);
}

/**
 * `({ verdict, reasonCode, reasonText, compact, method })`. Every status renders a lucide
 * icon and a text phrase: never a bare icon, and never the six-letter past-tense word this
 * component's quote_matches phrase deliberately avoids: a matching quote shows only that the
 * lines existed when they were read, never that the claim itself is true.
 *
 * For `not_checkable`, the badge shows "Cannot check" plus, at most, a short label from the
 * closed `NOT_CHECKABLE` record (e.g. "outside project"), never the backend's own
 * `reason` sentence, which can run to hundreds of characters and would clip inside this
 * `whitespace-nowrap` badge with no ellipsis. The full sentence (from `NOT_CHECKABLE`,
 * falling back to `reasonText`) is always available as the badge's `title`.
 */
export function VerdictBadge({
  verdict,
  reasonCode,
  reasonText,
  compact,
  method,
}: {
  verdict: BadgeStatus;
  reasonCode?: string | null;
  reasonText?: string | null;
  compact?: boolean;
  method?: string | null;
}) {
  const spec = SPEC[verdict];
  const reason = isNotCheckableReason(reasonCode) ? NOT_CHECKABLE[reasonCode] : null;
  const suffix = !compact && verdict === "not_checkable" && reason ? `: ${reason.short}` : "";
  const fullReason =
    verdict === "not_checkable" ? reason?.sentence || reasonText || undefined : undefined;
  const title = isSeeded({ method }) ? "Seeded demo stand-in" : fullReason;
  return (
    <Badge tone={spec.tone} title={title}>
      <spec.icon aria-hidden className="size-3.5" />
      {spec.phrase}
      {suffix}
    </Badge>
  );
}
