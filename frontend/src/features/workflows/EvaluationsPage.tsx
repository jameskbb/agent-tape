import { Plus } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";

import { describeError } from "@/api/client";
import { Page } from "@/components/layout/Page";
import {
  Badge,
  Button,
  Card,
  CodeBlock,
  EmptyState,
  ErrorState,
  LoadingBlock,
  RelativeTime,
  useToast,
} from "@/components/ui";

import {
  useCreateDefinition,
  useCreateDefinitionVersion,
  useCreateSuite,
  useEvaluationDefinitions,
  useEvaluationSuites,
  useEvaluatorTypes,
  type EvaluationDefinition,
} from "./api";
import { JsonField } from "./NodeForm";
import { Field, inputClass, textareaClass } from "./ui";

function NewDefinitionForm({ onDone }: { onDone: () => void }) {
  const toast = useToast();
  const types = useEvaluatorTypes();
  const create = useCreateDefinition();
  const [type, setType] = useState("regex");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [config, setConfig] = useState<unknown>({ pattern: "Traceback", mode: "absent" });
  const [required, setRequired] = useState(true);
  const info = types.data?.find((t) => t.type === type);
  const submit = (event: { preventDefault: () => void }) => {
    event.preventDefault();
    create.mutate(
      {
        name,
        description: description || undefined,
        evaluator_type: type,
        config: (config ?? {}) as Record<string, unknown>,
        required,
      },
      {
        onSuccess: () => {
          toast.show(`Saved ${name} as version 1`, "success");
          onDone();
        },
        onError: (e) => toast.show(describeError(e), "error"),
      },
    );
  };
  return (
    <form onSubmit={submit} className="grid gap-4 lg:grid-cols-2" aria-label="New evaluation">
      <div className="flex flex-col gap-3">
        <Field label="Name">
          {(props) => (
            <input
              {...props}
              required
              className={inputClass}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          )}
        </Field>
        <Field label="Evaluator" help={info?.description}>
          {(props) => (
            <select
              {...props}
              className={inputClass}
              value={type}
              onChange={(e) => setType(e.target.value)}
            >
              {(types.data ?? []).map((t) => (
                <option key={t.type} value={t.type}>
                  {t.display_name}
                  {t.available ? "" : " (unavailable)"}
                </option>
              ))}
            </select>
          )}
        </Field>
        {info && !info.available ? (
          <p className="text-sm text-[var(--st-cancelling)]">{info.unavailable_reason}</p>
        ) : null}
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
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={required}
            onChange={(e) => setRequired(e.target.checked)}
          />
          Required gate (a failure fails the run or the workflow gate)
        </label>
      </div>
      <div className="flex flex-col gap-3">
        <JsonField key={type} label="Configuration" value={config} onChange={setConfig} rows={6} />
        {info ? (
          <details className="text-sm">
            <summary className="cursor-pointer text-muted">Configuration schema</summary>
            <CodeBlock code={JSON.stringify(info.config_schema, null, 2)} />
          </details>
        ) : null}
      </div>
      <div className="flex gap-2 lg:col-span-2">
        <Button type="submit" variant="primary" loading={create.isPending}>
          Save evaluation
        </Button>
        <Button variant="ghost" onClick={onDone}>
          Cancel
        </Button>
      </div>
    </form>
  );
}

function DefinitionRow({ d }: { d: EvaluationDefinition }) {
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const [config, setConfig] = useState<unknown>(d.latest?.config ?? {});
  const [required, setRequired] = useState(d.latest?.required ?? false);
  const save = useCreateDefinitionVersion(d.id);
  return (
    <li className="px-4 py-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{d.name}</span>
        <Badge>{d.evaluator_type}</Badge>
        <Badge tone="accent">v{d.latest_version}</Badge>
        {d.latest?.required ? <Badge tone="violet">Required</Badge> : null}
        {d.deterministic ? (
          <Badge tone="success" title="Depends only on recorded data, so it can be rerun">
            Rerunnable
          </Badge>
        ) : null}
        {d.latest?.locked_at ? <Badge title="Used by an evaluation">In use</Badge> : null}
        <span className="ml-auto text-xs text-muted">
          Updated <RelativeTime iso={d.updated_at} />
        </span>
        <Button size="sm" variant="ghost" onClick={() => setEditing((v) => !v)}>
          {editing ? "Close" : "New version"}
        </Button>
      </div>
      {d.description ? <p className="mt-1 text-sm text-muted">{d.description}</p> : null}
      {editing ? (
        <div className="mt-3 flex flex-col gap-3">
          <JsonField label="Configuration" value={config} onChange={setConfig} rows={5} />
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={required}
              onChange={(e) => setRequired(e.target.checked)}
            />
            Required gate
          </label>
          <div>
            <Button
              variant="primary"
              size="sm"
              loading={save.isPending}
              onClick={() =>
                save.mutate(
                  { config: (config ?? {}) as Record<string, unknown>, required },
                  {
                    onSuccess: (v) => {
                      toast.show(`Saved ${d.name} version ${v.version}`, "success");
                      setEditing(false);
                    },
                    onError: (e) => toast.show(describeError(e), "error"),
                  },
                )
              }
            >
              Save version {d.latest_version + 1}
            </Button>
          </div>
        </div>
      ) : (
        <code className="mt-1 block truncate text-xs text-faint">
          {JSON.stringify(d.latest?.config ?? {})}
        </code>
      )}
    </li>
  );
}

function NewSuiteForm({
  definitions,
  onDone,
}: {
  definitions: EvaluationDefinition[];
  onDone: () => void;
}) {
  const toast = useToast();
  const create = useCreateSuite();
  const [name, setName] = useState("");
  const [chosen, setChosen] = useState<string[]>([]);
  return (
    <form
      aria-label="New suite"
      className="flex flex-col gap-3"
      onSubmit={(e) => {
        e.preventDefault();
        create.mutate(
          { name, members: chosen.map((id) => ({ definition_id: id })) },
          {
            onSuccess: () => {
              toast.show(`Created suite ${name}`, "success");
              onDone();
            },
            onError: (err) => toast.show(describeError(err), "error"),
          },
        );
      }}
    >
      <Field label="Suite name">
        {(props) => (
          <input
            {...props}
            required
            className={inputClass}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        )}
      </Field>
      <fieldset className="flex flex-col gap-1.5">
        <legend className="mb-1 text-sm font-medium text-ink-soft">
          Evaluations (latest version)
        </legend>
        {definitions.map((d) => (
          <label key={d.id} className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={chosen.includes(d.id)}
              onChange={(e) =>
                setChosen((c) => (e.target.checked ? [...c, d.id] : c.filter((x) => x !== d.id)))
              }
            />
            {d.name}
          </label>
        ))}
      </fieldset>
      <div className="flex gap-2">
        <Button
          type="submit"
          variant="primary"
          loading={create.isPending}
          disabled={!chosen.length}
        >
          Create suite
        </Button>
        <Button variant="ghost" onClick={onDone}>
          Cancel
        </Button>
      </div>
    </form>
  );
}

export function EvaluationsPage() {
  const definitions = useEvaluationDefinitions();
  const suites = useEvaluationSuites();
  const [adding, setAdding] = useState(false);
  const [addingSuite, setAddingSuite] = useState(false);
  const defs = definitions.data?.items ?? [];
  const names = new Map(defs.map((d) => [d.id, d.name]));
  return (
    <Page
      title="Evaluations"
      breadcrumb={
        <Link to="/workflows" className="hover:underline">
          Workflows
        </Link>
      }
      description="Reusable, versioned checks. Evaluation gates in workflows run them against a node's child run; you can also run a suite on any finished run from its Evaluations tab."
      actions={
        <Button
          variant="primary"
          icon={<Plus aria-hidden className="size-4" />}
          onClick={() => setAdding(true)}
        >
          New evaluation
        </Button>
      }
    >
      {adding ? (
        <Card title="New evaluation">
          <NewDefinitionForm onDone={() => setAdding(false)} />
        </Card>
      ) : null}
      <Card title="Definitions" padded={false}>
        {definitions.isPending ? (
          <LoadingBlock />
        ) : definitions.isError ? (
          <ErrorState error={definitions.error} onRetry={() => void definitions.refetch()} />
        ) : defs.length === 0 ? (
          <div className="p-4">
            <EmptyState
              title="No evaluations yet"
              description="Save a check such as a regex that must not match, a file that must exist, or a diff-size limit."
              action={<Button onClick={() => setAdding(true)}>New evaluation</Button>}
            />
          </div>
        ) : (
          <ul className="flex flex-col divide-y divide-line" aria-label="Evaluation definitions">
            {defs.map((d) => (
              <DefinitionRow key={`${d.id}-${d.latest_version}`} d={d} />
            ))}
          </ul>
        )}
      </Card>
      <Card
        title="Suites"
        description="Groups of evaluations run together, for example by a workflow gate."
        actions={
          <Button size="sm" onClick={() => setAddingSuite(true)} disabled={!defs.length}>
            New suite
          </Button>
        }
      >
        {addingSuite ? (
          <NewSuiteForm definitions={defs} onDone={() => setAddingSuite(false)} />
        ) : suites.data && suites.data.items.length ? (
          <ul className="flex flex-col gap-3" aria-label="Evaluation suites">
            {suites.data.items.map((s) => (
              <li key={s.id}>
                <p className="font-medium">{s.name}</p>
                {s.description ? <p className="text-sm text-muted">{s.description}</p> : null}
                <div className="mt-1 flex flex-wrap gap-1">
                  {s.members.map((m) => (
                    <Badge key={m.definition_id}>
                      {names.get(m.definition_id) ?? "Unknown"}
                      {m.version ? ` v${m.version}` : ""}
                    </Badge>
                  ))}
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-muted">No suites yet.</p>
        )}
      </Card>
    </Page>
  );
}
