import { FlaskConical, Plus, Search, Workflow as WorkflowIcon } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";

import type { RunStatus } from "@/api/types";
import { Page } from "@/components/layout/Page";
import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  ErrorState,
  LoadingBlock,
  RelativeTime,
  StatusPill,
  Unavailable,
  type Column,
} from "@/components/ui";

import { useWorkflows, type Workflow } from "./api";
import { inputClass, LinkButton } from "./ui";

const PAGE_SIZE = 25;

export function WorkflowsPage() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const offset = Number(params.get("offset") ?? 0) || 0;
  const [draft, setDraft] = useState(q);
  const query = useWorkflows({ q: q || undefined, offset, limit: PAGE_SIZE });

  const columns: Column<Workflow>[] = [
    {
      id: "name",
      header: "Workflow",
      cell: (w) => (
        <div className="min-w-0">
          <Link to={`/workflows/${w.id}`} className="font-medium text-ink hover:underline">
            {w.name}
          </Link>
          {w.description ? (
            <p className="line-clamp-1 max-w-[60ch] text-sm text-muted">{w.description}</p>
          ) : null}
        </div>
      ),
    },
    {
      id: "nodes",
      header: "Nodes",
      cell: (w) => (w.node_count === null ? <Unavailable reason="No version yet" /> : w.node_count),
      align: "right",
    },
    { id: "version", header: "Version", cell: (w) => `v${w.latest_version}`, align: "right" },
    {
      id: "last",
      header: "Last run",
      cell: (w) =>
        w.last_run_status && w.last_run_id ? (
          <Link to={`/runs/${w.last_run_id}`} aria-label={`Last run of ${w.name}`}>
            <StatusPill status={w.last_run_status as RunStatus} size="sm" />
          </Link>
        ) : (
          <span className="text-sm text-muted">Never run</span>
        ),
    },
    {
      id: "updated",
      header: "Updated",
      cell: (w) => (w.updated_at ? <RelativeTime iso={w.updated_at} /> : <Unavailable />),
    },
    {
      id: "tags",
      header: "Tags",
      cell: (w) => (
        <div className="flex flex-wrap gap-1">
          {w.is_demo ? <Badge tone="live">Demo</Badge> : null}
          {w.tags
            .filter((t) => t !== "demo")
            .map((t) => (
              <Badge key={t}>{t}</Badge>
            ))}
        </div>
      ),
    },
  ];

  const actions = (
    <>
      <LinkButton
        to="/workflows/evaluations"
        icon={<FlaskConical aria-hidden className="size-4" />}
      >
        Evaluations
      </LinkButton>
      <LinkButton
        to="/workflows/new"
        variant="primary"
        icon={<Plus aria-hidden className="size-4" />}
      >
        New workflow
      </LinkButton>
    </>
  );

  return (
    <Page
      title="Workflows"
      description="Versioned graphs of agent runs, commands, approvals, and evaluation gates. Every workflow run is a normal run with one step per node."
      actions={actions}
    >
      <form
        role="search"
        className="flex max-w-md items-center gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          setParams(draft ? { q: draft } : {});
        }}
      >
        <label className="relative flex-1">
          <span className="sr-only">Search workflows</span>
          <Search aria-hidden className="absolute top-2.5 left-2.5 size-4 text-faint" />
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="Search by name"
            className={`${inputClass} pl-8`}
          />
        </label>
        <Button type="submit">Search</Button>
      </form>
      {query.isPending ? (
        <LoadingBlock />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : query.data.total === 0 && !q ? (
        <EmptyState
          icon={<WorkflowIcon className="size-6" />}
          title="No workflows yet"
          description="Chain a preparation command, an agent run, an approval, and an evaluation gate. Run the demo seed to get the Safe Code Change workflow."
          action={
            <Button variant="primary" onClick={() => navigate("/workflows/new")}>
              New workflow
            </Button>
          }
        />
      ) : (
        <DataTable
          caption="Workflows"
          columns={columns}
          rows={query.data.items}
          rowKey={(w) => w.id}
          busy={query.isFetching}
          empty={<p className="p-4 text-muted">No workflows match "{q}".</p>}
          pagination={{
            offset,
            limit: PAGE_SIZE,
            total: query.data.total,
            onChange: (next) =>
              setParams((prev) => {
                const p = new URLSearchParams(prev);
                p.set("offset", String(next));
                return p;
              }),
          }}
        />
      )}
    </Page>
  );
}
