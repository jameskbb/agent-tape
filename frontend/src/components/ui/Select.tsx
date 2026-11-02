import type { ReactNode, SelectHTMLAttributes } from "react";

import { cn } from "@/lib/cn";

/** The compact control style shared by filter bars (selects, search inputs, text areas). */
export const controlClass =
  "rounded-[var(--radius-control)] border border-line-strong bg-surface px-2 py-1 text-sm text-ink";

export interface SelectOption {
  value: string;
  label: string;
}

/**
 * A compact native select for filters and inline choices. ``label`` is shown before the
 * control (pass ``aria-label`` too when the visible label is not a full name); ``any`` adds
 * a leading empty option such as "All".
 */
export function Select({
  label,
  options,
  any,
  value,
  onChange,
  className,
  ...rest
}: Omit<SelectHTMLAttributes<HTMLSelectElement>, "value" | "onChange"> & {
  label?: ReactNode;
  options: readonly SelectOption[];
  any?: string;
  value: string;
  onChange: (value: string) => void;
}) {
  const select = (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className={cn(controlClass, "min-w-0", className)}
      {...rest}
    >
      {any !== undefined ? <option value="">{any}</option> : null}
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
  if (label === undefined) return select;
  return (
    <label className="flex flex-wrap items-center gap-1.5 text-sm">
      <span className="text-muted">{label}</span>
      {select}
    </label>
  );
}
