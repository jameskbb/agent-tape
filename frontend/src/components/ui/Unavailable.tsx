import { cn } from "@/lib/cn";

/**
 * Honest placeholder for data that was not reported (token usage, cost, exit code...).
 * Never render 0 for unknown values; render this instead.
 */
export function Unavailable({
  reason = "Not reported",
  className,
}: {
  reason?: string;
  className?: string;
}) {
  return (
    <span
      className={cn("inline-flex items-center gap-1 text-sm text-faint italic", className)}
      title={reason}
      aria-label={reason}
      data-unavailable=""
    >
      <span aria-hidden className="inline-block h-px w-3 bg-current align-middle" />
      {reason}
    </span>
  );
}
