/** Run-detail "Runtime" tab for Herdr runs: where the agent lives, its lifecycle, and how
 * to open it. "Open in Herdr" is a copyable command; a browser cannot attach to a terminal. */
import { TERMINAL_STATUSES, type RunDetail } from "@/api/types";
import type { RunDetailTab } from "@/app/registry";
import { ErrorState, LoadingBlock } from "@/components/ui";

import { useRunRuntime } from "./api";
import { SessionCard } from "./SessionCard";

function RuntimeTab({ detail }: { detail: RunDetail }) {
  const live = !TERMINAL_STATUSES.has(detail.run.status);
  const view = useRunRuntime(detail.run.id, live);
  if (view.isPending) return <LoadingBlock label="Loading runtime" />;
  if (view.isError) return <ErrorState error={view.error} onRetry={() => void view.refetch()} />;
  if (!view.data.sessions.length)
    return (
      <p className="text-sm text-muted">
        No runtime session recorded yet. It appears as soon as Herdr creates the pane.
      </p>
    );
  return (
    <div className="flex flex-col gap-4" aria-label="Runtime sessions">
      {[...view.data.sessions].reverse().map((s) => (
        <SessionCard key={s.attempt_id} s={s} />
      ))}
    </div>
  );
}

export const runDetailTabs: RunDetailTab[] = [
  {
    id: "runtime",
    label: "Runtime",
    order: 35,
    isAvailable: (detail) => detail.run.adapter_type === "herdr",
    render: ({ detail }) => <RuntimeTab detail={detail} />,
  },
];
