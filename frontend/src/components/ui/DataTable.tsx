import { ArrowDown, ArrowUp, ChevronLeft, ChevronRight } from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "@/lib/cn";

import { IconButton } from "./Button";

export interface Column<T> {
  id: string;
  header: string;
  cell: (row: T) => ReactNode;
  /** Server-side sort key; omit for unsortable columns. */
  sortKey?: string;
  className?: string;
  align?: "left" | "right";
}

export interface SortState {
  key: string;
  order: "asc" | "desc";
}

export interface Pagination {
  offset: number;
  limit: number;
  total: number;
  onChange: (offset: number) => void;
}

/** Table with server-driven sorting and offset pagination. */
export function DataTable<T>({
  columns,
  rows,
  rowKey,
  sort,
  onSort,
  pagination,
  onRowClick,
  caption,
  empty,
  busy = false,
}: {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  sort?: SortState;
  onSort?: (sort: SortState) => void;
  pagination?: Pagination;
  onRowClick?: (row: T) => void;
  caption: string;
  empty?: ReactNode;
  busy?: boolean;
}) {
  const toggle = (key: string) => {
    if (!onSort) return;
    onSort(
      sort?.key === key
        ? { key, order: sort.order === "asc" ? "desc" : "asc" }
        : { key, order: "desc" },
    );
  };
  return (
    <div
      className="overflow-hidden rounded-[var(--radius-panel)] border border-line bg-surface"
      aria-busy={busy}
    >
      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-left text-sm">
          <caption className="sr-only">{caption}</caption>
          <thead className="bg-sunken text-muted">
            <tr>
              {columns.map((col) => {
                const active = sort?.key === col.sortKey;
                const ariaSort = active
                  ? sort?.order === "asc"
                    ? "ascending"
                    : "descending"
                  : undefined;
                return (
                  <th
                    key={col.id}
                    scope="col"
                    aria-sort={ariaSort}
                    className={cn(
                      "border-b border-line px-3 py-2 font-medium",
                      col.align === "right" && "text-right",
                      col.className,
                    )}
                  >
                    {col.sortKey && onSort ? (
                      <button
                        type="button"
                        onClick={() => toggle(col.sortKey ?? col.id)}
                        className={cn(
                          "inline-flex items-center gap-1 hover:text-ink",
                          active && "text-ink",
                        )}
                      >
                        {col.header}
                        {active ? (
                          sort?.order === "asc" ? (
                            <ArrowUp aria-hidden className="size-3" />
                          ) : (
                            <ArrowDown aria-hidden className="size-3" />
                          )
                        ) : null}
                      </button>
                    ) : (
                      col.header
                    )}
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td colSpan={columns.length} className="px-3 py-8 text-center text-muted">
                  {empty ?? "Nothing to show"}
                </td>
              </tr>
            ) : (
              rows.map((row) => (
                <tr
                  key={rowKey(row)}
                  onClick={onRowClick ? () => onRowClick(row) : undefined}
                  className={cn(
                    "border-b border-line last:border-b-0",
                    onRowClick && "cursor-pointer hover:bg-sunken",
                  )}
                >
                  {columns.map((col) => (
                    <td
                      key={col.id}
                      className={cn(
                        "px-3 py-2.5 align-middle",
                        col.align === "right" && "text-right tabular-nums",
                        col.className,
                      )}
                    >
                      {col.cell(row)}
                    </td>
                  ))}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
      {pagination && pagination.total > 0 ? <Pager {...pagination} /> : null}
    </div>
  );
}

function Pager({ offset, limit, total, onChange }: Pagination) {
  const from = Math.min(total, offset + 1);
  const to = Math.min(total, offset + limit);
  return (
    <nav
      aria-label="Pagination"
      className="flex items-center justify-between gap-2 border-t border-line px-3 py-2 text-sm text-muted"
    >
      <span className="tabular-nums">
        {from}–{to} of {total}
      </span>
      <div className="flex gap-1">
        <IconButton
          size="sm"
          label="Previous page"
          icon={<ChevronLeft className="size-4" />}
          disabled={offset === 0}
          onClick={() => onChange(Math.max(0, offset - limit))}
        />
        <IconButton
          size="sm"
          label="Next page"
          icon={<ChevronRight className="size-4" />}
          disabled={offset + limit >= total}
          onClick={() => onChange(offset + limit)}
        />
      </div>
    </nav>
  );
}
