import "@xyflow/react/dist/style.css";

import {
  applyEdgeChanges,
  applyNodeChanges,
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  type Connection,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import {
  Bot,
  Braces,
  FlaskConical,
  GitBranch,
  PackageCheck,
  Scale,
  ShieldCheck,
  SquareTerminal,
  type LucideIcon,
} from "lucide-react";
import { memo, useMemo, useState } from "react";

import type { RunStatus } from "@/api/types";
import { useTheme } from "@/app/theme";
import { STATUS_STYLES, statusLabel } from "@/components/ui";
import { cn } from "@/lib/cn";

import type { NodeType, WorkflowDefinition } from "./api";
import { edgeLabel, layout, TYPE_LABELS } from "./graph";

export const TYPE_ICONS: Record<NodeType, LucideIcon> = {
  agent_run: Bot,
  command: SquareTerminal,
  transform: Braces,
  evaluation_gate: FlaskConical,
  finding_review: Scale,
  approval: ShieldCheck,
  condition: GitBranch,
  collect: PackageCheck,
};

interface StepNodeData extends Record<string, unknown> {
  label: string;
  nodeKey: string;
  type: NodeType;
  status?: RunStatus;
  selected?: boolean;
  detail?: string;
}

type StepNode = Node<StepNodeData, "step">;

/**
 * One node, drawn like a module on a tape deck: a spine in the node's status colour on
 * the left, the node's name, its key in mono, and its type. Status is also written out,
 * never colour alone.
 */
const StepNodeView = memo(function StepNodeView({ data }: NodeProps<StepNode>) {
  const Icon = TYPE_ICONS[data.type];
  const style = data.status ? STATUS_STYLES[data.status] : undefined;
  const spine = style ? `var(--st-${style.var})` : "var(--line-strong)";
  return (
    <div
      data-node-key={data.nodeKey}
      data-status={data.status}
      className={cn(
        "relative flex w-[184px] overflow-hidden rounded-[var(--radius-control)] border bg-surface text-left shadow-sm",
        data.selected ? "border-accent ring-2 ring-accent-soft" : "border-line-strong",
      )}
    >
      <span aria-hidden className="w-1.5 shrink-0" style={{ background: spine }} />
      <div className="min-w-0 flex-1 px-2.5 py-2">
        <div className="flex items-center gap-1.5 text-xs text-muted">
          <Icon aria-hidden className="size-3.5 shrink-0" />
          <span className="truncate">{TYPE_LABELS[data.type]}</span>
        </div>
        <div className="mt-0.5 truncate text-sm font-semibold text-ink">{data.label}</div>
        <div className="flex items-center justify-between gap-2">
          <code className="truncate text-xs text-faint">{data.nodeKey}</code>
          {style ? (
            <span className="text-xs font-medium" style={{ color: spine }}>
              {statusLabel(data.status as RunStatus)}
            </span>
          ) : null}
        </div>
        {data.detail ? <p className="mt-1 truncate text-xs text-muted">{data.detail}</p> : null}
      </div>
      <Handle type="target" position={Position.Left} className="!bg-line-strong" />
      <Handle type="source" position={Position.Right} className="!bg-accent" />
    </div>
  );
});

const nodeTypes = { step: StepNodeView };

export function toFlow(
  definition: Pick<WorkflowDefinition, "nodes" | "edges">,
  statuses: Record<string, RunStatus> = {},
  selected?: string | null,
  details: Record<string, string> = {},
): { nodes: StepNode[]; edges: Edge[] } {
  const positions = layout(definition);
  const nodes: StepNode[] = definition.nodes.map((n) => ({
    id: n.key,
    type: "step",
    position: positions.get(n.key) ?? { x: 0, y: 0 },
    data: {
      label: n.name || n.key,
      nodeKey: n.key,
      type: n.type,
      status: statuses[n.key],
      selected: selected === n.key,
      detail: details[n.key],
    },
  }));
  const edges: Edge[] = definition.edges.map((e) => {
    const flowing = statuses[e.from] === "succeeded" && statuses[e.to] === "running";
    const skipped = statuses[e.to] === "skipped";
    const color = flowing ? "var(--accent)" : "var(--line-strong)";
    return {
      id: `${e.from}->${e.to}`,
      source: e.from,
      target: e.to,
      label: edgeLabel(e),
      animated: flowing,
      type: "smoothstep",
      style: { stroke: color, strokeWidth: 1.5, strokeDasharray: skipped ? "4 4" : undefined },
      markerEnd: { type: MarkerType.ArrowClosed, color },
      labelStyle: { fontSize: 11, fill: "var(--muted)" },
      labelBgStyle: { fill: "var(--surface)" },
    };
  });
  return { nodes, edges };
}

/**
 * The DAG. Read-only for runs (live statuses); editable in the editor (drag to move, drag
 * from a node's right handle to another node to connect, click to select). The list view
 * next to it is the accessible representation of the same graph.
 */
export function WorkflowGraph({
  definition,
  statuses,
  details,
  selected,
  editable = false,
  onSelect,
  onConnect,
  onMove,
  onDeleteEdges,
  height = 360,
}: {
  definition: Pick<WorkflowDefinition, "nodes" | "edges">;
  statuses?: Record<string, RunStatus>;
  details?: Record<string, string>;
  selected?: string | null;
  editable?: boolean;
  onSelect?: (key: string | null) => void;
  onConnect?: (from: string, to: string) => void;
  onMove?: (key: string, position: { x: number; y: number }) => void;
  onDeleteEdges?: (ids: string[]) => void;
  height?: number;
}) {
  const { resolved } = useTheme();
  const flow = useMemo(
    () => toFlow(definition, statuses, selected, details),
    [definition, statuses, selected, details],
  );
  // React Flow needs local state to show drags and edge selection between saves.
  const [nodes, setNodes] = useState(flow.nodes);
  const [edges, setEdges] = useState(flow.edges);
  const [synced, setSynced] = useState(flow);
  if (synced !== flow) {
    // A new definition or status map replaces the local copy (render-time sync).
    setSynced(flow);
    setNodes(flow.nodes);
    setEdges(flow.edges);
  }
  return (
    <figure
      className="m-0 overflow-hidden rounded-[var(--radius-panel)] border border-line bg-sunken"
      style={{ height }}
      aria-label="Workflow graph (the node list describes the same graph)"
    >
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={(changes) =>
          setNodes((ns) =>
            applyNodeChanges(
              changes.filter((c) => c.type !== "remove"),
              ns,
            ),
          )
        }
        onEdgesChange={(changes) =>
          setEdges((es) =>
            applyEdgeChanges(
              changes.filter((c) => c.type !== "remove"),
              es,
            ),
          )
        }
        nodeTypes={nodeTypes}
        colorMode={resolved}
        style={{ background: "var(--surface-sunken)" }}
        fitView
        fitViewOptions={{ padding: 0.12, maxZoom: 1.1, minZoom: 0.55 }}
        minZoom={0.3}
        nodesDraggable={editable}
        nodesConnectable={editable}
        elementsSelectable
        edgesFocusable={editable}
        deleteKeyCode={editable ? ["Backspace", "Delete"] : null}
        onNodeClick={(_, node) => onSelect?.(node.id)}
        onPaneClick={() => onSelect?.(null)}
        onConnect={(c: Connection) => {
          if (c.source && c.target && c.source !== c.target) onConnect?.(c.source, c.target);
        }}
        onNodeDragStop={(_, node) =>
          onMove?.(node.id, { x: Math.round(node.position.x), y: Math.round(node.position.y) })
        }
        onEdgesDelete={(edges) => onDeleteEdges?.(edges.map((e) => e.id))}
      >
        <Background gap={20} size={1} color="var(--line)" />
        <Controls showInteractive={false} />
      </ReactFlow>
    </figure>
  );
}
