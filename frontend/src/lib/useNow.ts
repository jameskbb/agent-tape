import { useEffect, useState } from "react";

/** The current time (epoch ms), re-read every `intervalMs` while `active`, so a
 * time-dependent label keeps advancing while it stays on screen. */
export function useNow(intervalMs: number, active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs, active]);
  return now;
}
