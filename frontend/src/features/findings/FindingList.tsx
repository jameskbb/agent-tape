/**
 * The recorded-order finding list (the caller filters by chip before passing it in; this
 * component never sorts), with roving-tabindex keyboard selection exactly like `Tabs`, and
 * the stacked `Select` picker used below `lg`.
 */
import { useRef, type KeyboardEvent } from "react";

import { Badge, Select } from "@/components/ui";
import { cn } from "@/lib/cn";
import { humanize } from "@/lib/format";

import type { Finding } from "./api";
import { badgeStatus, worstVerdict } from "./model";
import { VerdictBadge } from "./VerdictBadge";

function citationCountLabel(count: number): string {
  if (count === 0) return "No citation";
  return count === 1 ? "1 citation" : `${count} citations`;
}

function truncate60(text: string): string {
  return text.length > 60 ? text.slice(0, 60) : text;
}

export function FindingList({
  findings,
  selectedKey,
  onSelect,
  live,
}: {
  findings: Finding[];
  selectedKey: string | null;
  onSelect: (key: string) => void;
  live: boolean;
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const onKey = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const last = findings.length - 1;
    const next =
      event.key === "ArrowDown"
        ? index === last
          ? 0
          : index + 1
        : event.key === "ArrowUp"
          ? index === 0
            ? last
            : index - 1
          : event.key === "Home"
            ? 0
            : event.key === "End"
              ? last
              : null;
    if (next === null) return;
    event.preventDefault();
    const finding = findings[next];
    if (finding) {
      onSelect(finding.key);
      refs.current[next]?.focus();
    }
  };
  return (
    <ul aria-label="Findings" className="flex flex-col gap-1">
      {findings.map((finding, index) => {
        const selected = finding.key === selectedKey;
        return (
          <li key={finding.key}>
            <button
              type="button"
              ref={(el) => {
                refs.current[index] = el;
              }}
              aria-current={selected ? "true" : undefined}
              tabIndex={selected ? 0 : -1}
              onClick={() => onSelect(finding.key)}
              onKeyDown={(event) => onKey(event, index)}
              className={cn(
                "flex w-full flex-col items-start gap-1 border-l-[3px] px-3 py-3 text-left",
                selected ? "border-accent bg-sunken" : "border-transparent hover:bg-sunken",
              )}
            >
              <Badge tone="accent">{humanize(finding.kind)}</Badge>
              <span className="line-clamp-2 text-base font-medium text-ink [overflow-wrap:anywhere]">
                {finding.text}
              </span>
              <span className="flex flex-wrap items-center gap-2">
                <VerdictBadge verdict={badgeStatus(worstVerdict(finding), live)} compact />
                <span className="text-xs text-muted">
                  {citationCountLabel(finding.citations.length)}
                </span>
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

/** The below-`lg` stand-in for `FindingList`: one native `Select` whose option labels carry
 * the finding's position and claim, since there is no visible list to place it beside. */
export function FindingPicker({
  findings,
  selectedKey,
  onSelect,
}: {
  findings: Finding[];
  selectedKey: string | null;
  onSelect: (key: string) => void;
}) {
  const options = findings.map((finding, index) => ({
    value: finding.key,
    label: `Finding ${index + 1} of ${findings.length}: ${truncate60(finding.text ?? "")}`,
  }));
  return (
    <Select aria-label="Findings" options={options} value={selectedKey ?? ""} onChange={onSelect} />
  );
}
