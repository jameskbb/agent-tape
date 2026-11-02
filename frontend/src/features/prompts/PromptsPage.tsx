import { Download, Plus, Search, Upload } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router";

import { describeError } from "@/api/client";
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
  useToast,
  type Column,
  Unavailable,
} from "@/components/ui";
import { cn } from "@/lib/cn";

import {
  downloadJson,
  exportPrompts,
  usePromptTags,
  usePrompts,
  type PromptListParams,
  type PromptTemplate,
} from "./api";
import { ImportDialog } from "./ImportDialog";

const PAGE_SIZE = 25;

const columns: Column<PromptTemplate>[] = [
  {
    id: "name",
    header: "Prompt",
    sortKey: "name",
    cell: (t) => (
      <div className="min-w-[16rem]">
        <Link
          to={`/prompts/${t.id}`}
          className="font-medium text-ink hover:text-accent"
          onClick={(e) => e.stopPropagation()}
        >
          {t.name}
        </Link>
        {t.description ? (
          <p className="mt-0.5 line-clamp-1 max-w-[60ch] text-xs text-muted">{t.description}</p>
        ) : null}
      </div>
    ),
  },
  {
    id: "version",
    header: "Latest",
    cell: (t) => (
      <span className="inline-flex items-center gap-1.5">
        <span className="tabular-nums">v{t.latest_version}</span>
        {t.latest?.locked_at ? (
          <Badge title="Used by at least one run; this version can no longer change">Used</Badge>
        ) : null}
      </span>
    ),
  },
  {
    id: "variables",
    header: "Variables",
    align: "right",
    cell: (t) => (t.latest ? t.latest.variables.length : <Unavailable reason="No version yet" />),
  },
  {
    id: "tags",
    header: "Tags",
    cell: (t) => (
      <div className="flex flex-wrap gap-1">
        {t.tags.map((tag) => (
          <Badge key={tag}>{tag}</Badge>
        ))}
        {t.archived ? <Badge tone="warning">Archived</Badge> : null}
      </div>
    ),
  },
  { id: "owner", header: "Owner", cell: (t) => <span className="text-ink-soft">{t.owner}</span> },
  {
    id: "updated",
    header: "Updated",
    sortKey: "updated_at",
    cell: (t) => <RelativeTime iso={t.updated_at} className="text-ink-soft" />,
  },
];

export function PromptsPage() {
  const { params, update } = useUrlState();
  const navigate = useNavigate();
  const toast = useToast();
  const archivedParam = params.get("archived");
  const filters: PromptListParams = {
    q: params.get("q") ?? undefined,
    tag: params.getAll("tag"),
    archived: archivedParam === "archived" || archivedParam === "all" ? archivedParam : "active",
    sort: params.get("sort") ?? "name",
    order: params.get("order") === "desc" ? "desc" : "asc",
    offset: Number(params.get("offset") ?? 0) || 0,
    limit: PAGE_SIZE,
  };
  const prompts = usePrompts(filters);
  const tags = usePromptTags();
  const [search, setSearch] = useState(filters.q ?? "");
  const [importing, setImporting] = useState(false);
  const [exporting, setExporting] = useState(false);

  useEffect(() => {
    const id = setTimeout(() => {
      if ((filters.q ?? "") !== search) update({ q: search || null });
    }, 300);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- debounce on the input only
  }, [search]);

  const selectedTags = filters.tag ?? [];
  const anyFilter = Boolean(filters.q || selectedTags.length || filters.archived !== "active");

  const exportAll = async () => {
    setExporting(true);
    try {
      const bundle = await exportPrompts([]);
      downloadJson(bundle, "agenttape-prompts.json");
      toast.show(`Exported ${bundle.templates.length} prompts`, "success");
    } catch (e) {
      toast.show(describeError(e), "error");
    } finally {
      setExporting(false);
    }
  };

  const actions = (
    <>
      <Button icon={<Upload className="size-4" aria-hidden />} onClick={() => setImporting(true)}>
        Import
      </Button>
      <Button
        icon={<Download className="size-4" aria-hidden />}
        loading={exporting}
        onClick={() => void exportAll()}
      >
        Export all
      </Button>
      <Button
        variant="primary"
        icon={<Plus className="size-4" aria-hidden />}
        onClick={() => void navigate("/prompts/new")}
      >
        New prompt
      </Button>
    </>
  );

  return (
    <Page
      title="Prompts"
      description="Versioned prompt templates with typed variables. A version becomes read-only once a run has used it, so every run can be reproduced."
      actions={actions}
    >
      <div className="flex flex-col gap-3" role="search" aria-label="Filter prompts">
        <div className="flex flex-wrap items-center gap-2">
          <label className="relative flex min-w-[16rem] flex-1 items-center">
            <span className="sr-only">Search prompts</span>
            <Search aria-hidden className="absolute left-2.5 size-4 text-faint" />
            <input
              type="search"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search by name or description"
              className="h-9 w-full rounded-[var(--radius-control)] border border-line-strong bg-surface pr-2 pl-8 text-ink"
            />
          </label>
          <label className="flex items-center gap-2 text-sm text-muted">
            Show
            <select
              value={filters.archived}
              onChange={(e) =>
                update({ archived: e.target.value === "active" ? null : e.target.value })
              }
              className="h-9 rounded-[var(--radius-control)] border border-line-strong bg-surface px-2 text-ink"
            >
              <option value="active">Active</option>
              <option value="archived">Archived</option>
              <option value="all">All</option>
            </select>
          </label>
        </div>
        {tags.data && tags.data.length > 0 ? (
          <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Tags">
            {tags.data.map((tag) => {
              const on = selectedTags.includes(tag);
              return (
                <button
                  key={tag}
                  type="button"
                  aria-pressed={on}
                  onClick={() =>
                    update({
                      tag: on ? selectedTags.filter((t) => t !== tag) : [...selectedTags, tag],
                    })
                  }
                  className={cn(
                    "rounded-full border px-2.5 py-0.5 text-sm transition-colors",
                    on
                      ? "border-accent bg-accent-soft text-ink"
                      : "border-line bg-surface text-ink-soft hover:border-line-strong",
                  )}
                >
                  {tag}
                </button>
              );
            })}
            {anyFilter ? (
              <button
                type="button"
                onClick={() => {
                  setSearch("");
                  update({ q: null, tag: [], archived: null });
                }}
                className="px-2 text-sm text-accent hover:underline"
              >
                Clear filters
              </button>
            ) : null}
          </div>
        ) : null}
      </div>

      {prompts.isPending ? (
        <LoadingBlock label="Loading prompts" />
      ) : prompts.isError ? (
        <ErrorState error={prompts.error} onRetry={() => void prompts.refetch()} />
      ) : prompts.data.total === 0 && !anyFilter ? (
        <EmptyState
          title="No prompts yet"
          description="Write a prompt once, give its blanks typed variables, and reuse it across runs, schedules, and workflows. Or import prompts exported from another AgentTape."
          action={
            <>
              <Button variant="primary" onClick={() => void navigate("/prompts/new")}>
                New prompt
              </Button>
              <Button onClick={() => setImporting(true)}>Import</Button>
            </>
          }
        />
      ) : (
        <DataTable
          caption="Prompts"
          columns={columns}
          rows={prompts.data.items}
          rowKey={(t) => t.id}
          busy={prompts.isFetching}
          sort={{ key: filters.sort ?? "name", order: filters.order ?? "asc" }}
          onSort={(s) => update({ sort: s.key, order: s.order })}
          onRowClick={(t) => void navigate(`/prompts/${t.id}`)}
          empty="No prompts match these filters."
          pagination={{
            offset: filters.offset ?? 0,
            limit: PAGE_SIZE,
            total: prompts.data.total,
            onChange: (offset) => update({ offset: offset ? String(offset) : null }, false),
          }}
        />
      )}
      <ImportDialog open={importing} onClose={() => setImporting(false)} />
    </Page>
  );
}
