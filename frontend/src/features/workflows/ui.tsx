/** Small form and link helpers used across the workflows feature (match components/ui). */
import { useId, type ReactNode } from "react";
import { Link } from "react-router";

import { cn } from "@/lib/cn";

export const inputClass =
  "h-9 w-full rounded-[var(--radius-control)] border border-line-strong bg-surface px-2.5 text-base text-ink placeholder:text-faint";
export const textareaClass =
  "w-full rounded-[var(--radius-control)] border border-line-strong bg-surface px-2.5 py-2 text-base text-ink placeholder:text-faint";
export const monoAreaClass = cn(textareaClass, "font-mono text-sm");

/** A router link that looks like a Button (never a button nested inside a link). */
export function LinkButton({
  to,
  children,
  icon,
  variant = "secondary",
}: {
  to: string;
  children: ReactNode;
  icon?: ReactNode;
  variant?: "primary" | "secondary";
}) {
  return (
    <Link
      to={to}
      className={cn(
        "inline-flex h-9 shrink-0 items-center justify-center gap-2 rounded-[var(--radius-control)] border px-3.5 text-base font-medium whitespace-nowrap transition-[background-color,filter] duration-150",
        variant === "primary"
          ? "border-transparent bg-accent text-accent-ink hover:brightness-110"
          : "border-line-strong bg-surface text-ink hover:bg-sunken",
      )}
    >
      {icon}
      {children}
    </Link>
  );
}

/** Label + control + optional help/error, wired with ids for screen readers. */
export function Field({
  label,
  help,
  error,
  children,
  className,
}: {
  label: string;
  help?: ReactNode;
  error?: string | null;
  children: (props: {
    id: string;
    "aria-describedby"?: string;
    "aria-invalid"?: boolean;
  }) => ReactNode;
  className?: string;
}) {
  const id = useId();
  const helpId = `${id}-help`;
  return (
    <div className={cn("flex flex-col gap-1", className)}>
      <label htmlFor={id} className="text-sm font-medium text-ink-soft">
        {label}
      </label>
      {children({
        id,
        "aria-describedby": help || error ? helpId : undefined,
        "aria-invalid": error ? true : undefined,
      })}
      {error ? (
        <p id={helpId} className="text-sm text-[var(--st-failed)]">
          {error}
        </p>
      ) : help ? (
        <p id={helpId} className="text-xs text-muted">
          {help}
        </p>
      ) : null}
    </div>
  );
}
