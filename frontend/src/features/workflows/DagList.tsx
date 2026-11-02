import type { ReactNode } from "react";

import type { RunStatus } from "@/api/types";
import { StatusPill } from "@/components/ui";
import { cn } from "@/lib/cn";

import type { WorkflowDefinition } from "./api";
import { edgeLabel, topoOrder, TYPE_LABELS } from "./graph";
import { TYPE_ICONS } from "./WorkflowGraph";

/**
 * The DAG as an ordered list (dependency order). This is the accessible view of the
 * graph: each row names the node, its type, what it waits for, and (for runs) its status.
 */
export function DagList({
  definition,
  statuses,
  extra,
  selected,
  onSelect,
  label = "Workflow nodes in dependency order",
}: {
  definition: Pick<WorkflowDefinition, "nodes" | "edges">;
  statuses?: Record<string, RunStatus>;
  extra?: (key: string) => ReactNode;
  selected?: string | null;
  onSelect?: (key: string) => void;
  label?: string;
}) {
  const order = topoOrder(definition) ?? definition.nodes.map((n) => n.key);
  const byKey = new Map(definition.nodes.map((n) => [n.key, n]));
  return (
    <ol aria-label={label} className="flex flex-col divide-y divide-line">
      {order.map((key, index) => {
        const node = byKey.get(key);
        if (!node) return null;
        const Icon = TYPE_ICONS[node.type];
        const deps = definition.edges.filter((e) => e.to === key);
        const status = statuses?.[key];
        const body = (
          <>
            <span className="w-6 shrink-0 pt-0.5 text-right text-xs text-faint tabular-nums">
              {index + 1}
            </span>
            <Icon aria-hidden className="mt-0.5 size-4 shrink-0 text-muted" />
            <span className="min-w-0 flex-1">
              <span className="flex flex-wrap items-baseline gap-x-2">
                <span className="font-medium text-ink">{node.name || key}</span>
                <code className="text-xs text-ink-soft">{key}</code>
                <span className="text-xs text-muted">{TYPE_LABELS[node.type]}</span>
              </span>
              <span className="mt-0.5 block text-xs text-muted">
                {deps.length === 0
                  ? "Starts first"
                  : "After " +
                    deps
                      .map((e) => {
                        const cond = edgeLabel(e);
                        return cond ? `${e.from} (${cond})` : e.from;
                      })
                      .join(", ")}
                {node.required === false ? ", optional" : ""}
                {node.continue_on_failure ? ", may fail without stopping the workflow" : ""}
              </span>
              {extra ? extra(key) : null}
            </span>
            {status ? <StatusPill status={status} size="sm" className="shrink-0" /> : null}
          </>
        );
        return (
          <li key={key}>
            {onSelect ? (
              <button
                type="button"
                aria-current={selected === key ? "true" : undefined}
                onClick={() => onSelect(key)}
                className={cn(
                  "flex w-full items-start gap-2.5 px-3 py-2.5 text-left hover:bg-sunken",
                  selected === key && "bg-accent-soft",
                )}
              >
                {body}
              </button>
            ) : (
              <div className="flex items-start gap-2.5 px-3 py-2.5">{body}</div>
            )}
          </li>
        );
      })}
    </ol>
  );
}
