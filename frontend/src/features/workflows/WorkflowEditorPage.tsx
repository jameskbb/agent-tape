import { CheckCircle2, Plus, Save, TriangleAlert } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link, useBlocker, useNavigate, useParams } from "react-router";

import { describeError } from "@/api/client";
import { Page } from "@/components/layout/Page";
import {
  Button,
  Card,
  ConfirmDialog,
  ErrorState,
  LoadingBlock,
  Tabs,
  useToast,
} from "@/components/ui";

import {
  NODE_TYPES,
  useCreateWorkflow,
  useSaveVersion,
  useUpdateWorkflow,
  useWorkflow,
  validateDefinition,
  type NodeType,
  type WorkflowDefinition,
  type WorkflowNode,
  type WorkflowValidation,
} from "./api";
import { DagList } from "./DagList";
import { emptyDefinition, newNode, renameKey, topoOrder, TYPE_LABELS } from "./graph";
import { JsonField, NodeForm } from "./NodeForm";
import { Field, inputClass, textareaClass } from "./ui";
import { WorkflowGraph } from "./WorkflowGraph";

/** Keep only what the backend accepts (drop empty optional fields). */
function clean(def: WorkflowDefinition): WorkflowDefinition {
  return JSON.parse(JSON.stringify(def)) as WorkflowDefinition;
}

function Editor({
  initial,
  workflowId,
  initialName = "",
  initialDescription = "",
  initialArchived = false,
  latestVersion,
}: {
  initial: WorkflowDefinition;
  workflowId?: string;
  initialName?: string;
  initialDescription?: string;
  initialArchived?: boolean;
  latestVersion?: number;
}) {
  const toast = useToast();
  const navigate = useNavigate();
  const [def, setDef] = useState<WorkflowDefinition>(initial);
  const [selected, setSelected] = useState<string | null>(initial.nodes[0]?.key ?? null);
  const [panel, setPanel] = useState("node");
  const [addType, setAddType] = useState<NodeType>("agent_run");
  const [name, setName] = useState(initialName);
  const [description, setDescription] = useState(initialDescription);
  const [archived, setArchived] = useState(initialArchived);
  const [notes, setNotes] = useState("");
  const [removing, setRemoving] = useState<string | null>(null);
  const [validation, setValidation] = useState<WorkflowValidation | null>(null);
  const [checking, setChecking] = useState(false);
  const create = useCreateWorkflow();
  const saveVersion = useSaveVersion(workflowId ?? "");
  const updateWorkflow = useUpdateWorkflow(workflowId ?? "");

  // Saved details: what the server has for name, description, and archived.
  const [saved, setSaved] = useState({
    name: initialName,
    description: initialDescription,
    archived: initialArchived,
  });
  const detailsDirty =
    name !== saved.name || description !== saved.description || archived !== saved.archived;
  const graphDirty = def !== initial || notes !== "";
  const dirty = graphDirty || (workflowId ? detailsDirty : name !== "" || description !== "");

  // Leaving with unsaved work asks first, in the app and on reload or tab close. A save that
  // navigates away sets `leaving` so its own navigation is not blocked.
  const leaving = useRef(false);
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      dirty && !leaving.current && currentLocation.pathname !== nextLocation.pathname,
  );
  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const saveDetails = () => {
    if (!name.trim()) {
      toast.show("The workflow needs a name", "error");
      return;
    }
    updateWorkflow.mutate(
      { name: name.trim(), description, archived },
      {
        onSuccess: (w) => {
          setSaved({ name: w.name, description: w.description ?? "", archived: w.archived });
          setName(w.name);
          toast.show(
            w.archived && !saved.archived ? "Workflow archived" : "Workflow details saved",
            "success",
          );
        },
        onError: (e) => toast.show(describeError(e), "error"),
      },
    );
  };

  const update = (next: WorkflowDefinition) => {
    setDef(next);
    setValidation(null);
  };
  const node = def.nodes.find((n) => n.key === selected) ?? null;

  const connect = (from: string, to: string) => {
    if (def.edges.some((e) => e.from === from && e.to === to)) return;
    const edges = [...def.edges, { from, to }];
    if (topoOrder({ nodes: def.nodes, edges }) === null) {
      toast.show(`Connecting ${from} to ${to} would create a cycle`, "error");
      return;
    }
    update({ ...def, edges });
  };

  const addNode = () => {
    const created = newNode(
      addType,
      def.nodes.map((n) => n.key),
    );
    const edges = selected ? [...def.edges, { from: selected, to: created.key }] : def.edges;
    update({ ...def, nodes: [...def.nodes, created], edges });
    setSelected(created.key);
    setPanel("node");
  };

  const removeNode = (key: string) => {
    update({
      ...def,
      nodes: def.nodes.filter((n) => n.key !== key),
      edges: def.edges.filter((e) => e.from !== key && e.to !== key),
    });
    setSelected(null);
  };

  const validate = async (): Promise<WorkflowValidation | null> => {
    setChecking(true);
    try {
      const result = await validateDefinition(clean(def));
      setValidation(result);
      setPanel("checks");
      return result;
    } catch (e) {
      toast.show(describeError(e), "error");
      return null;
    } finally {
      setChecking(false);
    }
  };

  const save = async () => {
    if (!workflowId && !name.trim()) {
      toast.show("Give the workflow a name first", "error");
      setPanel("workflow");
      return;
    }
    const result = await validate();
    if (!result?.valid) return;
    const body = { definition: clean(def), notes: notes || undefined };
    if (workflowId) {
      saveVersion.mutate(body, {
        onSuccess: (v) => {
          toast.show(`Saved version ${v.version}`, "success");
          leaving.current = true;
          void navigate(`/workflows/${workflowId}`);
        },
        onError: (e) => toast.show(describeError(e), "error"),
      });
    } else {
      create.mutate(
        { name: name.trim(), description: description || undefined, ...body },
        {
          onSuccess: (w) => {
            toast.show("Workflow created as version 1", "success");
            leaving.current = true;
            void navigate(`/workflows/${w.id}`);
          },
          onError: (e) => toast.show(describeError(e), "error"),
        },
      );
    }
  };

  const errors = validation?.issues.filter((i) => i.severity === "error") ?? [];

  return (
    <Page
      title={workflowId ? `Edit ${saved.name}` : "New workflow"}
      breadcrumb={
        <Link
          to={workflowId ? `/workflows/${workflowId}` : "/workflows"}
          className="hover:underline"
        >
          {workflowId ? saved.name : "Workflows"}
        </Link>
      }
      description={
        workflowId
          ? `Saving creates version ${(latestVersion ?? 0) + 1}. Earlier versions and the runs that used them do not change.`
          : "Add nodes, connect them in the graph or with each node's Runs after list, then validate and save."
      }
      actions={
        <>
          <Button onClick={() => void validate()} loading={checking}>
            Validate
          </Button>
          <Button
            variant="primary"
            icon={<Save aria-hidden className="size-4" />}
            loading={create.isPending || saveVersion.isPending}
            onClick={() => void save()}
          >
            {workflowId ? "Save new version" : "Create workflow"}
          </Button>
        </>
      }
    >
      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_26rem]">
        <div className="flex min-w-0 flex-col gap-4">
          <div className="flex flex-wrap items-end gap-2">
            <Field label="Node type" className="w-56">
              {(props) => (
                <select
                  {...props}
                  className={inputClass}
                  value={addType}
                  onChange={(e) => setAddType(e.target.value as NodeType)}
                >
                  {NODE_TYPES.map((t) => (
                    <option key={t} value={t}>
                      {TYPE_LABELS[t]}
                    </option>
                  ))}
                </select>
              )}
            </Field>
            <Button icon={<Plus aria-hidden className="size-4" />} onClick={addNode}>
              {selected ? `Add after ${selected}` : "Add node"}
            </Button>
          </div>
          <WorkflowGraph
            definition={def}
            editable
            selected={selected}
            height={460}
            onSelect={(k) => {
              setSelected(k);
              if (k) setPanel("node");
            }}
            onConnect={connect}
            onMove={(key, position) =>
              setDef((d) => ({
                ...d,
                nodes: d.nodes.map((n) => (n.key === key ? { ...n, position } : n)),
              }))
            }
            onDeleteEdges={(ids) =>
              update({
                ...def,
                edges: def.edges.filter((e) => !ids.includes(`${e.from}->${e.to}`)),
              })
            }
          />
          <p className="text-xs text-muted">
            Drag from a node's right edge to another node to connect them. Select a connection and
            press Delete to remove it. The list below is the same graph in dependency order.
          </p>
          <Card title="Nodes" padded={false}>
            <DagList
              definition={def}
              selected={selected}
              onSelect={(k) => {
                setSelected(k);
                setPanel("node");
              }}
              label="Workflow nodes (select one to edit)"
            />
          </Card>
        </div>
        <Card padded>
          <Tabs
            label="Editor panels"
            value={panel}
            onChange={setPanel}
            items={[
              { id: "node", label: "Node" },
              { id: "workflow", label: "Workflow" },
              { id: "checks", label: "Checks", count: validation ? errors.length : undefined },
            ]}
          >
            {panel === "node" ? (
              node ? (
                <NodeForm
                  key={node.key}
                  node={node}
                  definition={def}
                  onChange={(n: WorkflowNode) =>
                    update({ ...def, nodes: def.nodes.map((x) => (x.key === node.key ? n : x)) })
                  }
                  onRename={(to) => {
                    update(renameKey(def, node.key, to));
                    setSelected(to);
                  }}
                  onEdges={(edges) => {
                    if (topoOrder({ nodes: def.nodes, edges }) === null) {
                      toast.show("That dependency would create a cycle", "error");
                      return;
                    }
                    update({ ...def, edges });
                  }}
                  onRemove={() => setRemoving(node.key)}
                />
              ) : (
                <p className="text-sm text-muted">Select a node in the graph or the list.</p>
              )
            ) : panel === "workflow" ? (
              <div className="flex flex-col gap-4">
                <>
                  <Field label="Name">
                    {(props) => (
                      <input
                        {...props}
                        className={inputClass}
                        value={name}
                        onChange={(e) => setName(e.target.value)}
                      />
                    )}
                  </Field>
                  <Field label="Description">
                    {(props) => (
                      <textarea
                        {...props}
                        rows={2}
                        className={textareaClass}
                        value={description}
                        onChange={(e) => setDescription(e.target.value)}
                      />
                    )}
                  </Field>
                  {workflowId ? (
                    <>
                      <label className="flex items-start gap-2 text-sm">
                        <input
                          type="checkbox"
                          className="mt-1"
                          checked={archived}
                          onChange={(e) => setArchived(e.target.checked)}
                        />
                        <span>
                          Archived
                          <span className="block text-xs text-muted">
                            Hidden from the workflow list. Its versions and runs are kept.
                          </span>
                        </span>
                      </label>
                      <div className="flex items-center gap-3">
                        <Button
                          size="sm"
                          disabled={!detailsDirty}
                          loading={updateWorkflow.isPending}
                          onClick={saveDetails}
                        >
                          Save details
                        </Button>
                        <span className="text-xs text-muted">
                          Name, description, and archiving save now and do not create a version.
                        </span>
                      </div>
                      <hr className="border-line" />
                    </>
                  ) : null}
                </>
                <Field
                  label="Run at most this many nodes at once"
                  help="Independent nodes run in parallel up to this limit."
                >
                  {(props) => (
                    <input
                      {...props}
                      type="number"
                      min={1}
                      max={64}
                      className={inputClass}
                      value={def.settings.max_parallel ?? 4}
                      onChange={(e) =>
                        update({
                          ...def,
                          settings: { ...def.settings, max_parallel: Number(e.target.value) || 1 },
                        })
                      }
                    />
                  )}
                </Field>
                <JsonField
                  label="Inputs"
                  value={def.inputs}
                  rows={6}
                  onChange={(v) =>
                    update({ ...def, inputs: (v ?? []) as WorkflowDefinition["inputs"] })
                  }
                  help='Typed run inputs, used in templates as {{ inputs.name }}. For example [{"name": "goal", "type": "multiline", "default": "..."}].'
                />
                <Field label="Version notes">
                  {(props) => (
                    <input
                      {...props}
                      className={inputClass}
                      value={notes}
                      onChange={(e) => setNotes(e.target.value)}
                      placeholder="What changed in this version"
                    />
                  )}
                </Field>
              </div>
            ) : (
              <div className="flex flex-col gap-3" role="status">
                {!validation ? (
                  <p className="text-sm text-muted">
                    Validate to check for cycles, missing settings, and templates that point at
                    nodes which have not run yet.
                  </p>
                ) : validation.valid ? (
                  <p className="flex items-center gap-2 text-sm text-[var(--st-succeeded)]">
                    <CheckCircle2 aria-hidden className="size-4" /> The workflow is valid. Order:{" "}
                    {validation.order?.join(", ")}
                  </p>
                ) : (
                  <ul className="flex flex-col gap-2" aria-label="Validation problems">
                    {validation.issues.map((issue, i) => (
                      <li key={i} className="flex gap-2 text-sm">
                        <TriangleAlert
                          aria-hidden
                          className="mt-0.5 size-4 shrink-0 text-[var(--st-failed)]"
                        />
                        <span>
                          {issue.message}
                          {issue.path ? (
                            <code className="ml-1 text-xs text-faint">{issue.path}</code>
                          ) : null}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </Tabs>
        </Card>
      </div>
      <ConfirmDialog
        open={removing !== null}
        title={`Remove node ${removing ?? ""}?`}
        description={(() => {
          const count = def.edges.filter((e) => e.from === removing || e.to === removing).length;
          return count
            ? `Its ${count} connection${count === 1 ? "" : "s"} will be removed too. Nothing is saved until you save a version.`
            : "Nothing is saved until you save a version.";
        })()}
        confirmLabel="Remove node"
        tone="danger"
        onConfirm={() => {
          if (removing) removeNode(removing);
          setRemoving(null);
        }}
        onClose={() => setRemoving(null)}
      />
      <ConfirmDialog
        open={blocker.state === "blocked"}
        title="Leave without saving?"
        description="Your changes to this workflow have not been saved and will be lost."
        confirmLabel="Leave without saving"
        tone="danger"
        onConfirm={() => blocker.proceed?.()}
        onClose={() => blocker.reset?.()}
      />
    </Page>
  );
}

export function NewWorkflowPage() {
  return <Editor initial={emptyDefinition()} />;
}

export function EditWorkflowPage() {
  const { workflowId = "" } = useParams();
  const workflow = useWorkflow(workflowId);
  if (workflow.isPending)
    return (
      <Page title="Edit workflow">
        <LoadingBlock />
      </Page>
    );
  if (workflow.isError)
    return (
      <Page title="Edit workflow">
        <ErrorState
          error={workflow.error}
          resource="workflow"
          backTo="/workflows"
          backLabel="Back to workflows"
          onRetry={() => void workflow.refetch()}
        />
      </Page>
    );
  const wf = workflow.data;
  const initial = wf.latest?.definition ?? emptyDefinition();
  return (
    <Editor
      key={wf.latest?.id}
      initial={initial}
      workflowId={wf.id}
      initialName={wf.name}
      initialDescription={wf.description ?? ""}
      initialArchived={wf.archived}
      latestVersion={wf.latest_version}
    />
  );
}
