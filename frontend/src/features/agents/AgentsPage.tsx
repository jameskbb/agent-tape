import { Plus, Search } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router";

import { useUrlState } from "@/components/forms";
import { Page } from "@/components/layout/Page";
import {
  Badge,
  Button,
  Card,
  DataTable,
  EmptyState,
  ErrorState,
  LoadingBlock,
  RelativeTime,
  Tabs,
  type Column,
} from "@/components/ui";

import { AdapterPanel } from "./AdapterPanel";
import { useAdapterDetails, useProfiles, type AgentProfile } from "./api";
import { ExecutablesPanel } from "./ExecutablesPanel";
import { SecretsPanel } from "./SecretsPanel";

const PAGE_SIZE = 25;
const TABS = [
  { id: "profiles", label: "Profiles" },
  { id: "adapters", label: "Adapters" },
  { id: "secrets", label: "Secret references" },
  { id: "executables", label: "Executable allowlist" },
];

function ProfilesTab() {
  const { params, update } = useUrlState();
  const navigate = useNavigate();
  const adapters = useAdapterDetails();
  const filters = {
    q: params.get("q") ?? undefined,
    adapter_type: params.get("adapter") ?? undefined,
    include_archived: params.get("archived") === "1",
    offset: Number(params.get("offset") ?? 0) || 0,
    limit: PAGE_SIZE,
  };
  const profiles = useProfiles(filters);
  const [search, setSearch] = useState(filters.q ?? "");
  useEffect(() => {
    const id = setTimeout(() => {
      if ((filters.q ?? "") !== search) update({ q: search || null });
    }, 300);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- debounce on the input only
  }, [search]);
  const names = new Map((adapters.data ?? []).map((a) => [a.name, a.display_name]));
  const anyFilter = Boolean(filters.q || filters.adapter_type || filters.include_archived);

  const columns: Column<AgentProfile>[] = [
    {
      id: "name",
      header: "Profile",
      cell: (p) => (
        <div className="min-w-[14rem]">
          <Link
            to={`/agents/${p.id}`}
            className="font-medium text-ink hover:text-accent"
            onClick={(e) => e.stopPropagation()}
          >
            {p.name}
          </Link>
          {p.description ? (
            <p className="mt-0.5 line-clamp-1 max-w-[60ch] text-xs text-muted">{p.description}</p>
          ) : null}
        </div>
      ),
    },
    {
      id: "adapter",
      header: "Adapter",
      cell: (p) => <Badge>{names.get(p.adapter_type) ?? p.adapter_type}</Badge>,
    },
    {
      id: "version",
      header: "Version",
      cell: (p) => <span className="tabular-nums">v{p.latest_version}</span>,
    },
    {
      id: "flags",
      header: "",
      cell: (p) => (
        <div className="flex gap-1">
          {p.archived ? <Badge tone="warning">Archived</Badge> : null}
          {p.is_demo ? <Badge>Demo data</Badge> : null}
        </div>
      ),
    },
    {
      id: "updated",
      header: "Updated",
      cell: (p) => <RelativeTime iso={p.updated_at} className="text-ink-soft" />,
    },
  ];

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2" role="search" aria-label="Filter profiles">
        <label className="relative flex min-w-[14rem] flex-1 items-center">
          <span className="sr-only">Search profiles</span>
          <Search aria-hidden className="absolute left-2.5 size-4 text-faint" />
          <input
            type="search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search by name"
            className="h-9 w-full rounded-[var(--radius-control)] border border-line-strong bg-surface pr-2 pl-8 text-ink"
          />
        </label>
        <label className="flex items-center gap-2 text-sm text-muted">
          Adapter
          <select
            value={filters.adapter_type ?? ""}
            onChange={(e) => update({ adapter: e.target.value || null })}
            className="h-9 rounded-[var(--radius-control)] border border-line-strong bg-surface px-2 text-ink"
          >
            <option value="">Any adapter</option>
            {(adapters.data ?? []).map((a) => (
              <option key={a.name} value={a.name}>
                {a.display_name}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-2 text-sm text-muted">
          <input
            type="checkbox"
            checked={filters.include_archived}
            onChange={(e) => update({ archived: e.target.checked ? "1" : null })}
            className="size-4 accent-[var(--accent)]"
          />
          Include archived
        </label>
      </div>
      {profiles.isPending ? (
        <LoadingBlock label="Loading profiles" />
      ) : profiles.isError ? (
        <ErrorState error={profiles.error} onRetry={() => void profiles.refetch()} />
      ) : profiles.data.total === 0 && !anyFilter ? (
        <EmptyState
          title="No agent profiles yet"
          description="A profile saves how to run an agent: which adapter, executable and arguments, model, permissions, environment, and limits. Every saved change becomes a new version."
          action={
            <Button variant="primary" onClick={() => void navigate("/agents/new")}>
              New profile
            </Button>
          }
        />
      ) : (
        <DataTable
          caption="Agent profiles"
          columns={columns}
          rows={profiles.data.items}
          rowKey={(p) => p.id}
          busy={profiles.isFetching}
          onRowClick={(p) => void navigate(`/agents/${p.id}`)}
          empty="No profiles match these filters."
          pagination={{
            offset: filters.offset,
            limit: PAGE_SIZE,
            total: profiles.data.total,
            onChange: (offset) => update({ offset: offset ? String(offset) : null }, false),
          }}
        />
      )}
    </div>
  );
}

export function AgentsPage() {
  const { params, update } = useUrlState();
  const navigate = useNavigate();
  const tab = TABS.some((t) => t.id === params.get("tab"))
    ? (params.get("tab") ?? "profiles")
    : "profiles";
  return (
    <Page
      title="Agents"
      description="Profiles describe how to run an agent. Adapters connect AgentTape to the Demo Agent, Claude Code, Codex CLI, or any allowlisted command."
      actions={
        <Button
          variant="primary"
          icon={<Plus className="size-4" aria-hidden />}
          onClick={() => void navigate("/agents/new")}
        >
          New profile
        </Button>
      }
    >
      <Card>
        <Tabs
          label="Agent sections"
          items={TABS}
          value={tab}
          onChange={(id) => update({ tab: id === "profiles" ? null : id, q: null, offset: null })}
        >
          {tab === "profiles" ? (
            <ProfilesTab />
          ) : tab === "adapters" ? (
            <AdapterPanel />
          ) : tab === "secrets" ? (
            <SecretsPanel />
          ) : (
            <ExecutablesPanel />
          )}
        </Tabs>
      </Card>
    </Page>
  );
}
