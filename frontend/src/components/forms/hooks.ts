import { useEffect, useState } from "react";
import { useSearchParams } from "react-router";

/** Returns ``value`` after it has been stable for ``ms`` milliseconds. */
export function useDebounced<T>(value: T, ms = 300): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return debounced;
}

/** URL search-param state for list pages (filters, sort, page) so views are shareable. */
export function useUrlState() {
  const [params, setParams] = useSearchParams();
  const update = (patch: Record<string, string | string[] | null>, resetOffset = true) => {
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        for (const [key, value] of Object.entries(patch)) {
          next.delete(key);
          if (Array.isArray(value)) value.forEach((v) => next.append(key, v));
          else if (value !== null && value !== "") next.set(key, value);
        }
        if (resetOffset && !("offset" in patch)) next.delete("offset");
        return next;
      },
      { replace: true },
    );
  };
  return { params, update };
}
