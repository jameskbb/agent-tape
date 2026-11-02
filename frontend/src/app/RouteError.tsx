import { AlertTriangle, Home, RotateCcw } from "lucide-react";
import { isRouteErrorResponse, Link, useRouteError } from "react-router";

import { describeError } from "@/api/client";
import { Page } from "@/components/layout/Page";
import { Button } from "@/components/ui";

import { NotFound } from "./NotFound";

function Recovery({ error }: { error: unknown }) {
  return (
    <div
      role="alert"
      className="flex flex-col gap-4 rounded-[var(--radius-panel)] border border-[color-mix(in_srgb,var(--st-failed)_35%,transparent)] bg-[var(--st-failed-bg)] p-5"
    >
      <div className="flex items-start gap-3">
        <AlertTriangle aria-hidden className="mt-0.5 size-5 shrink-0 text-[var(--st-failed)]" />
        <div className="min-w-0">
          <p className="font-semibold text-ink">This page stopped working</p>
          <p className="mt-1 text-sm break-words text-ink-soft">
            {describeError(error)}. Your runs and data are not affected: the error happened while
            showing this page. Reloading usually fixes it; if it keeps happening, the browser
            console has the details.
          </p>
        </div>
      </div>
      <div className="flex flex-wrap gap-2">
        <Button
          variant="primary"
          icon={<RotateCcw className="size-4" aria-hidden />}
          onClick={() => window.location.reload()}
        >
          Reload page
        </Button>
        <Link
          to="/"
          reloadDocument
          className="inline-flex h-9 items-center gap-2 rounded-[var(--radius-control)] border border-line-strong bg-surface px-3.5 font-medium text-ink hover:bg-sunken"
        >
          <Home className="size-4" aria-hidden />
          Go to Overview
        </Link>
      </div>
    </div>
  );
}

/** Rendered inside the Shell when a page throws, so navigation stays available. */
export function RouteError() {
  const error = useRouteError();
  if (isRouteErrorResponse(error) && error.status === 404) return <NotFound />;
  return (
    <Page title="Something went wrong">
      <Recovery error={error} />
    </Page>
  );
}

/** Last resort when the Shell itself fails: a standalone page with the same recovery. */
export function RootError() {
  const error = useRouteError();
  return (
    <main className="mx-auto max-w-2xl px-5 py-10">
      <h1 className="mb-4 text-2xl font-semibold">AgentTape could not load</h1>
      <Recovery error={error} />
    </main>
  );
}
