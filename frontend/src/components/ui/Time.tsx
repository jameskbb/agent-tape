import { useEffect, useState } from "react";

import { formatDateTime, formatDuration, formatRelative } from "@/lib/format";

import { Unavailable } from "./Unavailable";

function useNow(intervalMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

export function RelativeTime({
  iso,
  className,
}: {
  iso: string | null | undefined;
  className?: string;
}) {
  const now = useNow(30_000);
  if (!iso) return <Unavailable reason="Not yet" />;
  return (
    <time dateTime={iso} title={formatDateTime(iso)} className={className}>
      {formatRelative(iso, now)}
    </time>
  );
}

/**
 * Duration of a finished span, or a live-updating elapsed time when only ``start`` is known
 * and ``live`` is true.
 */
export function Duration({
  ms,
  start,
  live = false,
  className,
}: {
  ms?: number | null;
  start?: string | null;
  live?: boolean;
  className?: string;
}) {
  const now = useNow(live ? 1000 : 60_000);
  let value = ms ?? null;
  if (value === null && live && start) value = now - new Date(start).getTime();
  const text = formatDuration(value);
  if (text === null) return <Unavailable reason="Not measured" />;
  return <span className={className}>{text}</span>;
}
