import type { ReactNode } from "react";

export function Stat({
  label,
  value,
  detail,
}: {
  label: string;
  value: ReactNode;
  detail?: ReactNode;
}) {
  return (
    <div className="flex min-w-0 flex-col gap-1 rounded-[var(--radius-panel)] border border-line bg-surface px-4 py-3">
      <dt className="text-sm text-muted">{label}</dt>
      <dd className="text-2xl font-semibold text-ink tabular-nums">{value}</dd>
      {detail ? <dd className="text-sm text-muted">{detail}</dd> : null}
    </div>
  );
}
