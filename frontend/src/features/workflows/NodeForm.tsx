import { useQuery } from "@tanstack/react-query";
import { Trash2 } from "lucide-react";
import { useState } from "react";

import { api } from "@/api/client";
import type { Page as PageOf } from "@/api/types";
import { Button } from "@/components/ui";

import {
  useEvaluationSuites,
  type EdgeCondition,
  type WorkflowDefinition,
  type WorkflowNode,
} from "./api";
import { ancestors, isValidKey, TYPE_HINTS, TYPE_LABELS } from "./graph";
import { Field, inputClass, monoAreaClass, textareaClass } from "./ui";

interface Named {
  id: string;
  name: string;
}

function useNamed(path: string) {
  return useQuery({
    queryKey: ["named", path],
    queryFn: () => api<PageOf<Named>>(path, { query: { limit: 200 } }),
    staleTime: 30_000,
  });
}

/** JSON editor that commits only valid JSON (on blur) and says what is wrong otherwise. */
export function JsonField({
  label,
  value,
  onChange,
  help,
  rows = 4,
}: {
  label: string;
  value: unknown;
  onChange: (value: unknown) => void;
  help?: string;
  rows?: number;
}) {
  const [text, setText] = useState(() =>
    value === undefined ? "" : JSON.stringify(value, null, 2),
  );
  const [error, setError] = useState<string | null>(null);
  return (
    <Field label={label} help={help} error={error}>
      {(props) => (
        <textarea
          {...props}
          rows={rows}
          spellCheck={false}
          className={monoAreaClass}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onBlur={() => {
            if (text.trim() === "") {
              setError(null);
              onChange(undefined);
              return;
            }
            try {
              onChange(JSON.parse(text));
              setError(null);
            } catch (e) {
              setError(`Not valid JSON: ${(e as Error).message}`);
            }
          }}
        />
      )}
    </Field>
  );
}

function TextField({
  label,
  value,
  onChange,
  help,
  multiline = false,
  placeholder,
  type = "text",
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  help?: string;
  multiline?: boolean;
  placeholder?: string;
  type?: string;
}) {
  return (
    <Field label={label} help={help}>
      {(props) =>
        multiline ? (
          <textarea
            {...props}
            rows={4}
            className={textareaClass}
            placeholder={placeholder}
            value={value}
            onChange={(e) => onChange(e.target.value)}
          />
        ) : (
          <input
            {...props}
            type={type}
            className={inputClass}
            placeholder={placeholder}
            value={value}
            onChange={(e) => onChange(e.target.value)}
          />
        )
      }
    </Field>
  );
}

function Check({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <label className="flex items-center gap-2 text-sm">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      {label}
    </label>
  );
}

function str(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === undefined || value === null) return "";
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}

function num(raw: string): number | undefined {
  return raw === "" ? undefined : Number(raw);
}

function ConfigFields({
  node,
  definition,
  set,
}: {
  node: WorkflowNode;
  definition: WorkflowDefinition;
  set: (patch: Record<string, unknown>) => void;
}) {
  const c = node.config;
  const profiles = useNamed("/agent-profiles");
  const prompts = useNamed("/prompts");
  const suites = useEvaluationSuites();
  switch (node.type) {
    case "command":
      return (
        <>
          <Field
            label="Arguments (one per line)"
            help="The first line is the program. It must be fixed text; later lines may use {{ templates }}. No shell is involved."
          >
            {(props) => (
              <textarea
                {...props}
                rows={4}
                spellCheck={false}
                className={monoAreaClass}
                value={((c.argv as string[] | undefined) ?? []).join("\n")}
                onChange={(e) => set({ argv: e.target.value.split("\n") })}
                onBlur={(e) =>
                  set({ argv: e.target.value.split("\n").filter((line) => line.trim() !== "") })
                }
              />
            )}
          </Field>
          <TextField
            label="Working directory"
            value={str(c.cwd)}
            onChange={(v) => set({ cwd: v || undefined })}
            help="Defaults to the project repository. Must stay inside it or an AgentTape workspace."
          />
          <Check
            label="Elevated (always asks for approval before running)"
            checked={Boolean(c.elevated)}
            onChange={(v) => set({ elevated: v || undefined })}
          />
          <Check
            label="Parse stdout as JSON into outputs.json"
            checked={Boolean(c.parse_json)}
            onChange={(v) => set({ parse_json: v || undefined })}
          />
        </>
      );
    case "agent_run":
      return (
        <>
          <Field label="Agent profile">
            {(props) => (
              <select
                {...props}
                className={inputClass}
                value={str(c.agent_profile_id)}
                onChange={(e) => set({ agent_profile_id: e.target.value })}
              >
                <option value="">Choose an agent profile</option>
                {(profiles.data?.items ?? []).map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            )}
          </Field>
          <Field label="Prompt template" help="Its variables come from the node inputs below.">
            {(props) => (
              <select
                {...props}
                className={inputClass}
                value={str(c.prompt_template_id)}
                onChange={(e) => set({ prompt_template_id: e.target.value || undefined })}
              >
                <option value="">Inline prompt instead</option>
                {(prompts.data?.items ?? []).map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            )}
          </Field>
          {!c.prompt_template_id ? (
            <TextField
              label="Inline prompt"
              multiline
              value={str(c.prompt)}
              onChange={(v) => set({ prompt: v })}
              placeholder="Summarise {{ nodes.prepare.outputs.stdout }}"
            />
          ) : null}
          <JsonField
            key={`evaluators-${node.key}`}
            label="Evaluators for the child run"
            value={c.evaluators}
            onChange={(v) => set({ evaluators: v })}
            help='Run on the child agent run when it finishes. For example [{"type": "regex", "name": "Mentions tests", "config": {"pattern": "test"}}]. Leave empty for none.'
          />
        </>
      );
    case "transform":
      return (
        <>
          <TextField
            label="Template (rendered into outputs.text)"
            multiline
            value={str(c.template)}
            onChange={(v) => set({ template: v })}
          />
          <JsonField
            label="Values (object rendered into outputs)"
            value={c.values}
            onChange={(v) => set({ values: v })}
          />
        </>
      );
    case "evaluation_gate": {
      const agents = definition.nodes.filter((n) => n.type === "agent_run");
      return (
        <>
          <Field
            label="Evaluate"
            help="Without a target the gate evaluates the workflow run itself."
          >
            {(props) => (
              <select
                {...props}
                className={inputClass}
                value={str(c.target)}
                onChange={(e) => set({ target: e.target.value || undefined })}
              >
                <option value="">The workflow run</option>
                {agents.map((n) => (
                  <option key={n.key} value={n.key}>
                    Child run of {n.name || n.key}
                  </option>
                ))}
              </select>
            )}
          </Field>
          <Field label="Evaluation suite">
            {(props) => (
              <select
                {...props}
                className={inputClass}
                value={str(c.suite_id)}
                onChange={(e) => set({ suite_id: e.target.value || undefined })}
              >
                <option value="">No suite</option>
                {(suites.data?.items ?? []).map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
              </select>
            )}
          </Field>
          <JsonField
            label="Inline evaluators"
            value={c.evaluators}
            onChange={(v) => set({ evaluators: v })}
            help='For example [{"type": "regex", "name": "No tracebacks", "config": {"pattern": "Traceback", "mode": "absent"}}]. Required unless "required": false.'
          />
          <Check
            label="Allow an approved override when a required evaluation fails"
            checked={Boolean(c.allow_override)}
            onChange={(v) => set({ allow_override: v || undefined })}
          />
        </>
      );
    }
    case "approval":
      return (
        <>
          <TextField label="Title" value={str(c.title)} onChange={(v) => set({ title: v })} />
          <TextField
            label="Rationale"
            multiline
            value={str(c.rationale)}
            onChange={(v) => set({ rationale: v || undefined })}
          />
          <Field label="Risk level">
            {(props) => (
              <select
                {...props}
                className={inputClass}
                value={str(c.risk_level) || "medium"}
                onChange={(e) => set({ risk_level: e.target.value })}
              >
                {["low", "medium", "high", "critical"].map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
              </select>
            )}
          </Field>
          <TextField
            label="Expires after (seconds)"
            type="number"
            value={str(c.expires_in_seconds)}
            onChange={(v) => set({ expires_in_seconds: num(v) })}
            help="Leave empty to wait indefinitely. An expired approval fails the node."
          />
        </>
      );
    case "finding_review": {
      const upstream = ancestors(definition, node.key);
      const targets = definition.nodes.filter((n) => upstream.has(n.key) && n.type === "agent_run");
      const noTargets = targets.length === 0;
      return (
        <>
          <Field
            label="Target node"
            help={
              noTargets
                ? "Add an edge from an agent run node first; a finding review challenges an " +
                  "upstream agent run's findings."
                : "The agent run whose findings to challenge"
            }
          >
            {(props) => (
              <select
                {...props}
                className={inputClass}
                disabled={noTargets}
                value={str(c.target)}
                onChange={(e) => set({ target: e.target.value })}
              >
                <option value="">Choose the agent run node</option>
                {targets.map((n) => (
                  <option key={n.key} value={n.key}>
                    {n.name || n.key}
                  </option>
                ))}
              </select>
            )}
          </Field>
          <Field label="Reviewer agent">
            {(props) => (
              <select
                {...props}
                className={inputClass}
                value={str(c.agent_profile_id)}
                onChange={(e) => set({ agent_profile_id: e.target.value })}
              >
                <option value="">Choose a reviewer agent profile</option>
                {(profiles.data?.items ?? []).map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            )}
          </Field>
          <TextField
            label="Agent version (optional)"
            value={str(c.agent_profile_version_id)}
            onChange={(v) => set({ agent_profile_version_id: v || undefined })}
            help="Pin a profile version. Leave empty to use the latest."
          />
        </>
      );
    }
    case "condition":
      return (
        <TextField
          label="Expression"
          value={str(c.expression)}
          onChange={(v) => set({ expression: v })}
          help="For example: nodes.evaluate.outputs.passed == true and inputs.count > 2"
        />
      );
    case "collect":
      return (
        <>
          <TextField
            label="Include nodes (comma separated)"
            value={((c.include as string[] | undefined) ?? []).join(", ")}
            onChange={(v) =>
              set({
                include: v
                  .split(",")
                  .map((s) => s.trim())
                  .filter(Boolean),
              })
            }
            help="Defaults to every upstream node."
          />
          <TextField
            label="Artifact name"
            value={str(c.artifact_name)}
            onChange={(v) => set({ artifact_name: v || undefined })}
            placeholder="workflow-output.json"
          />
        </>
      );
  }
}

function Dependencies({
  node,
  definition,
  onChange,
}: {
  node: WorkflowNode;
  definition: WorkflowDefinition;
  onChange: (edges: WorkflowDefinition["edges"]) => void;
}) {
  const others = definition.nodes.filter((n) => n.key !== node.key);
  const incoming = definition.edges.filter((e) => e.to === node.key);
  const setCondition = (from: string, patch: Partial<EdgeCondition>) =>
    onChange(
      definition.edges.map((e) => {
        if (e.from !== from || e.to !== node.key) return e;
        const next = Object.fromEntries(
          Object.entries({ ...(e.condition ?? {}), ...patch } as Record<string, unknown>).filter(
            ([, v]) => v !== undefined,
          ),
        ) as EdgeCondition;
        return { ...e, condition: Object.keys(next).length ? next : undefined };
      }),
    );
  return (
    <fieldset className="flex flex-col gap-2">
      <legend className="mb-1 text-sm font-medium text-ink-soft">Runs after</legend>
      {others.length === 0 ? <p className="text-sm text-muted">No other nodes yet.</p> : null}
      {others.map((o) => {
        const edge = incoming.find((e) => e.from === o.key);
        return (
          <div
            key={o.key}
            className="rounded-[var(--radius-control)] border border-line px-2.5 py-2"
          >
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={Boolean(edge)}
                onChange={(e) =>
                  onChange(
                    e.target.checked
                      ? [...definition.edges, { from: o.key, to: node.key }]
                      : definition.edges.filter((x) => !(x.from === o.key && x.to === node.key)),
                  )
                }
              />
              <span className="font-medium">{o.name || o.key}</span>
              <code className="text-xs text-faint">{o.key}</code>
            </label>
            {edge ? (
              <div className="mt-2 grid gap-2 sm:grid-cols-2">
                <label className="flex flex-col gap-1 text-xs text-muted">
                  When {o.key}
                  <select
                    className={inputClass}
                    value={edge.condition?.on ?? "success"}
                    onChange={(e) =>
                      setCondition(o.key, {
                        on:
                          e.target.value === "success"
                            ? undefined
                            : (e.target.value as EdgeCondition["on"]),
                      })
                    }
                  >
                    <option value="success">succeeds</option>
                    <option value="failure">fails</option>
                    <option value="always">finishes either way</option>
                  </select>
                </label>
                {o.type === "condition" ? (
                  <label className="flex flex-col gap-1 text-xs text-muted">
                    Branch
                    <select
                      className={inputClass}
                      value={
                        edge.condition?.branch === undefined ? "" : String(edge.condition.branch)
                      }
                      onChange={(e) =>
                        setCondition(o.key, {
                          branch: e.target.value === "" ? undefined : e.target.value === "true",
                        })
                      }
                    >
                      <option value="">Either result</option>
                      <option value="true">If true</option>
                      <option value="false">If false</option>
                    </select>
                  </label>
                ) : null}
                <label className="flex flex-col gap-1 text-xs text-muted sm:col-span-2">
                  And only when (optional expression)
                  <input
                    className={inputClass}
                    value={edge.condition?.when ?? ""}
                    placeholder={`nodes.${o.key}.outputs.passed == true`}
                    onChange={(e) => setCondition(o.key, { when: e.target.value || undefined })}
                  />
                </label>
              </div>
            ) : null}
          </div>
        );
      })}
    </fieldset>
  );
}

/** Everything about one node: identity, type-specific config, dependencies, behaviour. */
export function NodeForm({
  node,
  definition,
  onChange,
  onRename,
  onEdges,
  onRemove,
}: {
  node: WorkflowNode;
  definition: WorkflowDefinition;
  onChange: (node: WorkflowNode) => void;
  onRename: (to: string) => void;
  onEdges: (edges: WorkflowDefinition["edges"]) => void;
  onRemove: () => void;
}) {
  const [keyDraft, setKeyDraft] = useState(node.key);
  const taken = definition.nodes.some((n) => n.key === keyDraft && n.key !== node.key);
  const keyError = !isValidKey(keyDraft)
    ? "Start with a letter; use letters, digits, and underscores."
    : taken
      ? "Another node already uses this key."
      : null;
  const set = (patch: Record<string, unknown>) => {
    const config = Object.fromEntries(
      Object.entries({ ...node.config, ...patch }).filter(([, v]) => v !== undefined),
    );
    onChange({ ...node, config });
  };
  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <p className="text-sm font-semibold">{TYPE_LABELS[node.type]}</p>
          <p className="text-xs text-muted">{TYPE_HINTS[node.type]}</p>
        </div>
        <Button
          size="sm"
          variant="danger"
          icon={<Trash2 aria-hidden className="size-3.5" />}
          onClick={onRemove}
        >
          Remove node
        </Button>
      </div>
      <TextField
        label="Name"
        value={node.name ?? ""}
        onChange={(v) => onChange({ ...node, name: v })}
      />
      <Field
        label="Key"
        error={keyError}
        help="Used in templates as nodes.<key>.outputs. Renaming updates references."
      >
        {(props) => (
          <input
            {...props}
            className={`${inputClass} font-mono text-sm`}
            value={keyDraft}
            onChange={(e) => setKeyDraft(e.target.value)}
            onBlur={() => {
              if (!keyError && keyDraft !== node.key) onRename(keyDraft);
            }}
          />
        )}
      </Field>
      <ConfigFields node={node} definition={definition} set={set} />
      <JsonField
        key={`inputs-${node.key}`}
        label="Node inputs"
        value={node.inputs}
        onChange={(v) => onChange({ ...node, inputs: v as Record<string, unknown> | undefined })}
        help='Resolved before the node runs, e.g. {"files": "{{ nodes.prepare.outputs.stdout_lines }}"}. Agent runs pass them as prompt variables.'
      />
      <Dependencies node={node} definition={definition} onChange={onEdges} />
      <div className="grid grid-cols-2 gap-3">
        <TextField
          label="Timeout (seconds)"
          type="number"
          value={str(node.timeout_seconds)}
          onChange={(v) => onChange({ ...node, timeout_seconds: num(v) })}
        />
        <TextField
          label="Max attempts"
          type="number"
          value={str(node.max_attempts ?? 1)}
          onChange={(v) => onChange({ ...node, max_attempts: num(v) })}
        />
        <TextField
          label="Retry backoff (seconds)"
          type="number"
          value={str(node.retry_backoff_seconds)}
          placeholder="5"
          onChange={(v) => onChange({ ...node, retry_backoff_seconds: num(v) })}
          help="Wait before a retry. It doubles after each failed attempt. Defaults to 5."
        />
      </div>
      <Check
        label="Required (a failure stops the workflow)"
        checked={node.required !== false}
        onChange={(v) => onChange({ ...node, required: v ? undefined : false })}
      />
      <Check
        label="Continue on failure"
        checked={Boolean(node.continue_on_failure)}
        onChange={(v) => onChange({ ...node, continue_on_failure: v || undefined })}
      />
      <details className="text-sm">
        <summary className="cursor-pointer text-muted">Input and output schemas</summary>
        <div className="mt-2 flex flex-col gap-3">
          <JsonField
            key={`in-${node.key}`}
            label="Input schema (JSON Schema)"
            value={node.input_schema}
            onChange={(v) =>
              onChange({ ...node, input_schema: v as Record<string, unknown> | undefined })
            }
          />
          <JsonField
            key={`out-${node.key}`}
            label="Output schema (JSON Schema)"
            value={node.output_schema}
            onChange={(v) =>
              onChange({ ...node, output_schema: v as Record<string, unknown> | undefined })
            }
          />
        </div>
      </details>
    </div>
  );
}
