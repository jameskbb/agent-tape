import { AlertTriangle } from "lucide-react";
import { Link } from "react-router";

import { ApiError, describeError } from "@/api/client";

import { Button } from "./Button";
import { EmptyState } from "./EmptyState";

/**
 * A failed load. A 404 is not a server error: it renders a "does not exist" state with a
 * way back instead of a retry that can never succeed.
 */
export function ErrorState({
  error,
  onRetry,
  title = "This could not be loaded",
  resource = "item",
  backTo = "/",
  backLabel = "Go to Overview",
}: {
  error: unknown;
  onRetry?: () => void;
  title?: string;
  /** Noun for the not-found message, e.g. "schedule". */
  resource?: string;
  backTo?: string;
  backLabel?: string;
}) {
  if (error instanceof ApiError && error.status === 404) {
    return (
      <EmptyState
        title={`This ${resource} does not exist`}
        description="It may have been deleted, or the link is out of date."
        action={
          <Link to={backTo} className="text-accent underline-offset-2 hover:underline">
            {backLabel}
          </Link>
        }
      />
    );
  }
  return (
    <div
      role="alert"
      className="flex items-start gap-3 rounded-[var(--radius-panel)] border border-[color-mix(in_srgb,var(--st-failed)_35%,transparent)] bg-[var(--st-failed-bg)] p-4"
    >
      <AlertTriangle aria-hidden className="mt-0.5 size-5 shrink-0 text-[var(--st-failed)]" />
      <div className="min-w-0 flex-1">
        <p className="font-semibold text-ink">{title}</p>
        <p className="mt-0.5 text-sm break-words text-ink-soft">{describeError(error)}</p>
      </div>
      {onRetry ? (
        <Button size="sm" onClick={onRetry}>
          Try again
        </Button>
      ) : null}
    </div>
  );
}
