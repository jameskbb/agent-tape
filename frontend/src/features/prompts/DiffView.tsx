import { Badge } from "@/components/ui";
import { cn } from "@/lib/cn";

import type { PromptDiff } from "./api";

/** Line diff between two prompt versions, with variable-level changes summarised above. */
export function DiffView({ diff }: { diff: PromptDiff }) {
  const added = diff.lines.filter((l) => l.op === "add").length;
  const removed = diff.lines.filter((l) => l.op === "remove").length;
  const varChanges =
    diff.variables_added.length + diff.variables_removed.length + diff.variables_changed.length;
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="font-medium">
          v{diff.from_version} to v{diff.to_version}
        </span>
        <span className="text-[var(--st-succeeded)] tabular-nums">+{added}</span>
        <span className="text-[var(--st-failed)] tabular-nums">-{removed}</span>
        {!diff.body_changed ? <span className="text-muted">Template text unchanged</span> : null}
      </div>
      {varChanges ? (
        <div className="flex flex-wrap items-center gap-1.5" aria-label="Variable changes">
          <span className="text-xs text-muted">Variables:</span>
          {diff.variables_added.map((v) => (
            <Badge key={`a-${v}`} tone="success">
              + {v}
            </Badge>
          ))}
          {diff.variables_removed.map((v) => (
            <Badge key={`r-${v}`} tone="danger">
              - {v}
            </Badge>
          ))}
          {diff.variables_changed.map((v) => (
            <Badge key={`c-${v}`} tone="warning">
              ~ {v}
            </Badge>
          ))}
        </div>
      ) : null}
      <div
        className="overflow-x-auto rounded-[var(--radius-control)] border border-line bg-sunken"
        role="table"
        aria-label={`Diff from version ${diff.from_version} to ${diff.to_version}`}
      >
        <div role="rowgroup" className="min-w-full font-mono text-[0.8125rem] leading-relaxed">
          {diff.lines.length === 0 ? (
            <p className="px-3 py-2 text-muted">Both versions have an empty template.</p>
          ) : (
            diff.lines.map((line, i) => (
              <div
                key={i}
                role="row"
                data-op={line.op}
                className={cn(
                  "grid grid-cols-[3rem_3rem_1.25rem_minmax(0,1fr)]",
                  line.op === "add" && "bg-[var(--st-succeeded-bg)]",
                  line.op === "remove" && "bg-[var(--st-failed-bg)]",
                )}
              >
                <span role="cell" className="pr-2 text-right text-faint select-none">
                  {line.old_no ?? ""}
                </span>
                <span role="cell" className="pr-2 text-right text-faint select-none">
                  {line.new_no ?? ""}
                </span>
                <span
                  role="cell"
                  aria-label={
                    line.op === "add" ? "added" : line.op === "remove" ? "removed" : "unchanged"
                  }
                  className={cn(
                    "text-center select-none",
                    line.op === "add" && "text-[var(--st-succeeded)]",
                    line.op === "remove" && "text-[var(--st-failed)]",
                  )}
                >
                  {line.op === "add" ? "+" : line.op === "remove" ? "-" : " "}
                </span>
                <span role="cell" className="pr-3 whitespace-pre-wrap break-words text-ink-soft">
                  {line.text || " "}
                </span>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
