import type { UseQueryResult } from "@tanstack/react-query";
import { CircleCheck, CircleAlert, TriangleAlert } from "lucide-react";

import { useDebounced } from "@/components/forms";

import { useValidateConfig, type ProfileConfig, type ValidationIssue } from "./api";

export type ValidationQuery = UseQueryResult<{ valid: boolean; issues: ValidationIssue[] }>;

/** Debounced server-side validation (executable, allowlist, working dir, env, limits...). */
export function useLiveValidation(adapterType: string, config: ProfileConfig) {
  const debounced = useDebounced(config, 350);
  return useValidateConfig(adapterType, debounced, true);
}

export function ValidationSummary({ query }: { query: ValidationQuery }) {
  if (query.isPending) {
    return <p className="text-sm text-muted">Checking the configuration...</p>;
  }
  if (query.isError) {
    return (
      <p className="text-sm text-[var(--st-failed)]">
        Validation could not run. Saving will validate again.
      </p>
    );
  }
  const errors = query.data.issues.filter((i) => i.severity === "error").length;
  const warnings = query.data.issues.length - errors;
  return (
    <p className="flex items-center gap-1.5 text-sm" aria-live="polite">
      {errors ? (
        <>
          <CircleAlert aria-hidden className="size-4 text-[var(--st-failed)]" />
          <span className="text-[var(--st-failed)]">
            {errors} problem{errors === 1 ? "" : "s"} to fix before saving
          </span>
        </>
      ) : warnings ? (
        <>
          <TriangleAlert aria-hidden className="size-4 text-[var(--st-cancelling)]" />
          <span>
            Valid, with {warnings} warning{warnings === 1 ? "" : "s"}
          </span>
        </>
      ) : (
        <>
          <CircleCheck aria-hidden className="size-4 text-[var(--st-succeeded)]" />
          <span>Valid: executable, paths, environment, and limits check out</span>
        </>
      )}
      {query.isFetching ? <span className="text-muted">(rechecking)</span> : null}
    </p>
  );
}
