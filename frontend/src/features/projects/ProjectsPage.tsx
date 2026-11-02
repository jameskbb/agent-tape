import { FolderGit2, Plus, Search } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router";

import { useUrlState } from "@/components/forms";
import { Page } from "@/components/layout/Page";
import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  ErrorState,
  LoadingBlock,
  RelativeTime,
  type Column,
} from "@/components/ui";

import { AllowedRootsCard } from "./AllowedRootsCard";
import { useProjects, type Project } from "./api";
import { shortHead } from "./PathCheckView";

const PAGE_SIZE = 25;

const columns: Column<Project>[] = [
  {
    id: "name",
    header: "Project",
    cell: (p) => (
      <div className="max-w-[34rem] min-w-[14rem]">
        <Link
          to={`/projects/${p.id}`}
          className="font-medium text-ink hover:text-accent"
          onClick={(e) => e.stopPropagation()}
        >
          {p.name}
        </Link>
        <p className="mt-0.5 truncate font-mono text-xs text-muted" title={p.repo_path}>
          {p.repo_path}
        </p>
      </div>
    ),
  },
  {
    id: "git",
    header: "Git",
    cell: (p) =>
      p.git_state?.is_repo ? (
        <span className="inline-flex flex-wrap items-center gap-1.5 text-sm">
          <FolderGit2 aria-hidden className="size-3.5 text-muted" />
          {p.git_state.branch ?? "detached"}
          <code className="text-xs text-muted">{shortHead(p.git_state.head)}</code>
          {p.git_state.dirty ? (
            <Badge tone="warning">{p.git_state.dirty_count} uncommitted</Badge>
          ) : null}
        </span>
      ) : (
        <span className="text-sm text-faint">Not a Git repository</span>
      ),
  },
  {
    id: "flags",
    header: "",
    cell: (p) => (
      <div className="flex flex-wrap gap-1">
        {p.settings.git_isolation ? <Badge tone="accent">Isolated worktrees</Badge> : null}
        {p.archived ? <Badge tone="warning">Archived</Badge> : null}
        {p.is_demo ? <Badge>Demo data</Badge> : null}
        {p.tags.map((t) => (
          <Badge key={t}>{t}</Badge>
        ))}
      </div>
    ),
  },
  {
    id: "updated",
    header: "Updated",
    cell: (p) => <RelativeTime iso={p.updated_at} className="text-ink-soft" />,
  },
];

export function ProjectsPage() {
  const { params, update } = useUrlState();
  const navigate = useNavigate();
  const filters = {
    q: params.get("q") ?? undefined,
    include_archived: params.get("archived") === "1",
    offset: Number(params.get("offset") ?? 0) || 0,
    limit: PAGE_SIZE,
  };
  const projects = useProjects(filters);
  const [search, setSearch] = useState(filters.q ?? "");
  const [showRoots, setShowRoots] = useState(false);
  useEffect(() => {
    const id = setTimeout(() => {
      if ((filters.q ?? "") !== search) update({ q: search || null });
    }, 300);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- debounce on the input only
  }, [search]);
  const anyFilter = Boolean(filters.q || filters.include_archived);

  return (
    <Page
      title="Projects"
      description="Repositories AgentTape may work in. Every path is canonicalized and must sit inside an allowlisted root; AgentTape never runs anywhere else."
      actions={
        <>
          <Button aria-expanded={showRoots} onClick={() => setShowRoots((s) => !s)}>
            Allowed roots
          </Button>
          <Button
            variant="primary"
            icon={<Plus className="size-4" aria-hidden />}
            onClick={() => void navigate("/projects/new")}
          >
            Add project
          </Button>
        </>
      }
    >
      {showRoots ? <AllowedRootsCard /> : null}
      <div className="flex flex-wrap items-center gap-2" role="search" aria-label="Filter projects">
        <label className="relative flex min-w-[14rem] flex-1 items-center">
          <span className="sr-only">Search projects</span>
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
          <input
            type="checkbox"
            checked={filters.include_archived}
            onChange={(e) => update({ archived: e.target.checked ? "1" : null })}
            className="size-4 accent-[var(--accent)]"
          />
          Include archived
        </label>
      </div>
      {projects.isPending ? (
        <LoadingBlock label="Loading projects" />
      ) : projects.isError ? (
        <ErrorState error={projects.error} onRetry={() => void projects.refetch()} />
      ) : projects.data.total === 0 && !anyFilter ? (
        <EmptyState
          title="No projects yet"
          description="Add a repository from an allowlisted root. AgentTape records its Git state, and your settings (timeouts, protected paths, validation commands) apply to every run in it."
          action={
            <Button variant="primary" onClick={() => void navigate("/projects/new")}>
              Add project
            </Button>
          }
        />
      ) : (
        <DataTable
          caption="Projects"
          columns={columns}
          rows={projects.data.items}
          rowKey={(p) => p.id}
          busy={projects.isFetching}
          onRowClick={(p) => void navigate(`/projects/${p.id}`)}
          empty="No projects match these filters."
          pagination={{
            offset: filters.offset,
            limit: PAGE_SIZE,
            total: projects.data.total,
            onChange: (offset) => update({ offset: offset ? String(offset) : null }, false),
          }}
        />
      )}
    </Page>
  );
}
