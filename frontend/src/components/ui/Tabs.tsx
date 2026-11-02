import { useId, useRef, type KeyboardEvent, type ReactNode } from "react";

import { cn } from "@/lib/cn";

export interface TabItem {
  id: string;
  label: ReactNode;
  /** A number is shown as a `TabCount` chip; any other node (for example a query-backed
   * count component) is rendered as given. */
  count?: ReactNode;
}

/** The count chip `Tabs` renders for a numeric `count`; also used directly by count components. */
export function TabCount({ value }: { value: number }) {
  return <span className="rounded bg-sunken px-1 text-xs text-muted tabular-nums">{value}</span>;
}

/** Accessible tabs (roving tabindex, arrow keys). Controlled: pass ``value`` and ``onChange``. */
export function Tabs({
  items,
  value,
  onChange,
  children,
  label,
}: {
  items: TabItem[];
  value: string;
  onChange: (id: string) => void;
  children: ReactNode;
  label: string;
}) {
  const base = useId();
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const onKey = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const last = items.length - 1;
    const next =
      event.key === "ArrowRight"
        ? index === last
          ? 0
          : index + 1
        : event.key === "ArrowLeft"
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
    const item = items[next];
    if (item) {
      onChange(item.id);
      refs.current[next]?.focus();
    }
  };
  return (
    <div>
      <div
        role="tablist"
        aria-label={label}
        className="flex gap-1 overflow-x-auto border-b border-line"
      >
        {items.map((item, index) => {
          const selected = item.id === value;
          return (
            <button
              key={item.id}
              ref={(el) => {
                refs.current[index] = el;
              }}
              role="tab"
              type="button"
              id={`${base}-tab-${item.id}`}
              aria-selected={selected}
              aria-controls={`${base}-panel`}
              tabIndex={selected ? 0 : -1}
              onClick={() => onChange(item.id)}
              onKeyDown={(e) => onKey(e, index)}
              className={cn(
                "relative -mb-px flex items-center gap-1.5 border-b-2 px-3 py-2 text-sm font-medium whitespace-nowrap transition-colors",
                selected
                  ? "border-accent text-ink"
                  : "border-transparent text-muted hover:text-ink",
              )}
            >
              {item.label}
              {typeof item.count === "number" ? <TabCount value={item.count} /> : item.count}
            </button>
          );
        })}
      </div>
      <div
        role="tabpanel"
        id={`${base}-panel`}
        aria-labelledby={`${base}-tab-${value}`}
        tabIndex={0}
        className="pt-4 focus-visible:outline-none"
      >
        {children}
      </div>
    </div>
  );
}
