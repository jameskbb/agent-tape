/** A labelled row of source chips (Supports, Conflicts, Missing); each chip opens the source drawer. */
import { Badge } from "@/components/ui";
import type { LinkedSource } from "@/features/findings/api";
import { humanize } from "@/lib/format";

export function SourceChips({
  label,
  items,
  tone,
  onOpen,
}: {
  label: string;
  items: LinkedSource[];
  tone: "success" | "danger" | "warning";
  onOpen: (id: string | null) => void;
}) {
  if (items.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5 text-sm">
      <Badge tone={tone}>{label}</Badge>
      {items.map((src) => (
        <button
          key={src.key}
          type="button"
          onClick={() => onOpen(src.source_id)}
          className="rounded border border-line px-1.5 py-px text-left hover:bg-sunken"
          title={`${humanize(src.type)} · ${src.live === true ? "live" : src.live === false ? "snapshot" : "freshness unknown"} · ${humanize(src.origin)}`}
        >
          {src.name}
          <span className="ml-1 text-xs text-muted">
            {src.live === true ? "live" : src.live === false ? "snapshot" : ""}
          </span>
        </button>
      ))}
    </div>
  );
}
