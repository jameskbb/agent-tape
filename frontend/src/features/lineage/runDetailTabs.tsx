/** "Lineage" run-detail tab: parent and child runs (replays and forks) and what differed. */
import { Link } from "react-router";

import type { RunDetail } from "@/api/types";
import type { RunDetailTab } from "@/app/registry";
import {
  Badge,
  Card,
  EmptyState,
  ErrorState,
  LoadingBlock,
  RelativeTime,
  StatusPill,
} from "@/components/ui";
import { humanize, sourceLabel } from "@/lib/format";

import { useLineage, type LineageRun } from "./api";
import { DriftList } from "./ForkDialog";

function show(value: unknown): string {
  if (value === null || value === undefined) return "none";
  return typeof value === "string" ? value : JSON.stringify(value);
}

function RunLine({ run, compareHref }: { run: LineageRun; compareHref: string }) {
  return (
    <li className="flex flex-wrap items-center gap-3 py-2">
      <StatusPill status={run.status} size="sm" />
      <Badge tone={run.relation === "fork" ? "violet" : "accent"}>
        {humanize(run.relation ?? sourceLabel(run.source))}
      </Badge>
      <Link
        to={`/runs/${run.id}`}
        className="min-w-0 flex-1 truncate font-medium hover:text-accent"
      >
        {run.title}
      </Link>
      <RelativeTime iso={run.created_at} className="text-sm text-muted" />
      <Link to={compareHref} className="text-sm text-accent hover:underline">
        Compare
      </Link>
    </li>
  );
}

function LineageTab({ detail }: { detail: RunDetail }) {
  const query = useLineage(detail.run.id);
  if (query.isPending) return <LoadingBlock label="Loading lineage" />;
  if (query.isError) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  const data = query.data;
  const recorded = data.fork_of?.drift ?? data.replay_of?.drift ?? [];
  if (!data.parent && data.children.length === 0) {
    return (
      <EmptyState
        title="No replays or forks yet"
        description="Replay this run with the same configuration, or fork it with a different prompt version, agent profile, or variables. Related runs appear here and can be compared side by side."
      />
    );
  }
  return (
    <div className="flex flex-col gap-4">
      {data.parent ? (
        <Card
          title={
            detail.run.parent_relation === "fork"
              ? "Forked from"
              : detail.run.parent_relation === "replay"
                ? "Replay of"
                : "Parent run"
          }
        >
          <ul>
            <RunLine
              run={{ ...data.parent, relation: detail.run.parent_relation }}
              compareHref={`/runs/compare?a=${data.parent.id}&b=${detail.run.id}`}
            />
          </ul>
          {data.fork_of?.changes.length ? (
            <div className="mt-3">
              <h3 className="text-sm font-medium">What this fork changed</h3>
              <ul className="mt-1 flex flex-col gap-1 text-sm">
                {data.fork_of.changes.map((c) => (
                  <li key={c.field}>
                    <code>{c.field}</code>:{" "}
                    <span className="text-muted line-through">{show(c.from)}</span>{" "}
                    <span className="text-ink">{show(c.to)}</span>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          {recorded.length ? (
            <div className="mt-3">
              <DriftList drift={recorded} />
            </div>
          ) : null}
        </Card>
      ) : null}
      {data.children.length ? (
        <Card title="Replays and forks of this run">
          <ul className="divide-y divide-line">
            {data.children.map((c) => (
              <RunLine
                key={c.id}
                run={c}
                compareHref={`/runs/compare?a=${detail.run.id}&b=${c.id}`}
              />
            ))}
          </ul>
        </Card>
      ) : null}
    </div>
  );
}

export const runDetailTabs: RunDetailTab[] = [
  {
    id: "lineage",
    label: "Lineage",
    order: 45,
    isAvailable: (d) => d.run.kind === "agent" || Boolean(d.run.parent_run_id),
    count: (d) => (d.run.parent_run_id ? 1 : 0) + d.children.length || undefined,
    render: ({ detail }) => <LineageTab detail={detail} />,
  },
];
