import { cn } from "@/lib/cn";

const WINDOWS = [7, 14, 30] as const;

export function WindowSwitch({
  value,
  onChange,
}: {
  value: number;
  onChange: (days: number) => void;
}) {
  return (
    <div
      role="radiogroup"
      aria-label="Time window"
      className="inline-flex rounded-[var(--radius-control)] border border-line bg-sunken p-0.5"
    >
      {WINDOWS.map((d) => (
        <button
          key={d}
          type="button"
          role="radio"
          aria-checked={value === d}
          onClick={() => onChange(d)}
          className={cn(
            "rounded-[4px] px-2.5 py-1 text-sm",
            value === d ? "bg-surface font-medium text-ink shadow-sm" : "text-muted hover:text-ink",
          )}
        >
          {d} days
        </button>
      ))}
    </div>
  );
}
