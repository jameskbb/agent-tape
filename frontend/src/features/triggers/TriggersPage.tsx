import { useMutation, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Plus, Trash2, Webhook } from "lucide-react";
import { useId, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";

import { api, describeError } from "@/api/client";
import { Page } from "@/components/layout/Page";
import {
  Badge,
  Button,
  Card,
  CodeBlock,
  DataTable,
  EmptyState,
  ErrorState,
  IconButton,
  LoadingBlock,
  RelativeTime,
  useToast,
  type Column,
} from "@/components/ui";
import { cn } from "@/lib/cn";
import {
  scheduleKeys,
  useScheduleTargets,
  useTriggers,
  type ScheduleTargets,
  type Trigger,
  type TriggerInput,
  type TriggerWithToken,
} from "@/features/schedules/api";
import { Field, inputClass } from "@/features/schedules/parts";

import { curlExample } from "./examples";

const columns: Column<Trigger>[] = [
  {
    id: "state",
    header: "State",
    cell: (t) => (t.enabled ? <Badge tone="success">Enabled</Badge> : <Badge>Disabled</Badge>),
  },
  {
    id: "name",
    header: "Trigger",
    cell: (t) => (
      <div>
        <Link
          to={`/settings/triggers/${t.id}`}
          className="font-medium text-ink hover:text-accent"
          onClick={(e) => e.stopPropagation()}
        >
          {t.name}
        </Link>
        <span className="block text-xs text-muted">
          Runs {t.target_name ?? "a deleted target"}
          {t.agent_profile_name ? ` with ${t.agent_profile_name}` : ""}
        </span>
      </div>
    ),
  },
  {
    id: "token",
    header: "Token",
    cell: (t) => (t.token_prefix ? <code className="text-sm">{t.token_prefix}…</code> : "None"),
  },
  {
    id: "last",
    header: "Last delivery",
    cell: (t) =>
      t.last_delivery_at ? (
        <RelativeTime iso={t.last_delivery_at} className="text-ink-soft" />
      ) : (
        <span className="text-faint">Never</span>
      ),
  },
  { id: "count", header: "Deliveries", align: "right", cell: (t) => t.delivery_count },
];

const PAGE_SIZE = 25;

export function TriggersPage() {
  const [params, setParams] = useSearchParams();
  const offset = Number(params.get("offset") ?? 0) || 0;
  const triggers = useTriggers({ offset, limit: PAGE_SIZE });
  const targets = useScheduleTargets();
  const navigate = useNavigate();
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<TriggerWithToken | null>(null);

  const newButton = (
    <Button
      variant="primary"
      icon={<Plus aria-hidden className="size-4" />}
      onClick={() => {
        setCreated(null);
        setCreating(true);
      }}
    >
      New trigger
    </Button>
  );

  return (
    <Page
      title="Webhook triggers"
      breadcrumb={
        <Link to="/settings" className="hover:text-ink">
          Settings
        </Link>
      }
      description="Let another system start a saved prompt by sending JSON to a private URL. Each trigger has its own token, and every delivery is logged."
      actions={creating ? null : newButton}
    >
      {created ? (
        <TokenReveal
          created={created}
          onDone={() => void navigate(`/settings/triggers/${created.trigger.id}`)}
        />
      ) : null}
      {creating && !created ? (
        targets.data ? (
          <CreateTrigger
            targets={targets.data}
            onCancel={() => setCreating(false)}
            onCreated={(result) => {
              setCreating(false);
              setCreated(result);
            }}
          />
        ) : targets.isError ? (
          <ErrorState error={targets.error} onRetry={() => void targets.refetch()} />
        ) : (
          <LoadingBlock />
        )
      ) : null}
      {triggers.isPending ? (
        <LoadingBlock label="Loading triggers" />
      ) : triggers.isError ? (
        <ErrorState error={triggers.error} onRetry={() => void triggers.refetch()} />
      ) : triggers.data.total === 0 ? (
        creating || created ? null : (
          <EmptyState
            icon={<Webhook aria-hidden className="size-8" />}
            title="No webhook triggers yet"
            description="Create one to start runs from GitHub, CI, or any script that can send an HTTP request."
            action={newButton}
          />
        )
      ) : (
        <DataTable
          caption="Webhook triggers"
          columns={columns}
          rows={triggers.data.items}
          rowKey={(t) => t.id}
          onRowClick={(t) => void navigate(`/settings/triggers/${t.id}`)}
          pagination={{
            offset,
            limit: PAGE_SIZE,
            total: triggers.data.total,
            onChange: (next) =>
              setParams(
                (p) => {
                  if (next) p.set("offset", String(next));
                  else p.delete("offset");
                  return p;
                },
                { replace: true },
              ),
          }}
        />
      )}
    </Page>
  );
}

export function TokenReveal({
  created,
  onDone,
}: {
  created: TriggerWithToken;
  onDone: () => void;
}) {
  const url = `${window.location.origin}${created.trigger.webhook_path}`;
  return (
    <Card
      title={
        <span className="inline-flex items-center gap-2">
          <KeyRound aria-hidden className="size-4 text-[var(--st-cancelling)]" />
          Copy this token now
        </span>
      }
      description="AgentTape stores only a hash of it, so this is the only time it is shown. If you lose it, rotate the token."
      className="border-[color-mix(in_srgb,var(--st-cancelling)_45%,transparent)]"
    >
      <div className="flex flex-col gap-3">
        <CodeBlock code={created.token} label="Webhook token" />
        <CodeBlock code={url} label="Webhook URL" />
        <CodeBlock
          code={curlExample(url, created.trigger.input_mapping, created.token)}
          label="Try it with curl"
        />
        <div className="flex justify-end">
          <Button variant="primary" onClick={onDone}>
            I've stored the token
          </Button>
        </div>
      </div>
    </Card>
  );
}

interface MappingRow {
  variable: string;
  path: string;
}

function CreateTrigger({
  targets,
  onCancel,
  onCreated,
}: {
  targets: ScheduleTargets;
  onCancel: () => void;
  onCreated: (result: TriggerWithToken) => void;
}) {
  const client = useQueryClient();
  const toast = useToast();
  const ids = { name: useId(), prompt: useId(), profile: useId() };
  const [name, setName] = useState("");
  const [promptId, setPromptId] = useState("");
  const [profileId, setProfileId] = useState("");
  const [projectId, setProjectId] = useState("");
  const [rows, setRows] = useState<MappingRow[]>([]);
  const [idempotency, setIdempotency] = useState<"derive" | "require">("derive");
  const [keyPath, setKeyPath] = useState("");
  const [rate, setRate] = useState("30");
  const [maxKb, setMaxKb] = useState("256");
  const [error, setError] = useState<string | null>(null);
  const prompt = targets.prompts.find((p) => p.id === promptId);
  const mappable = (prompt?.variables ?? []).filter((v) => v.type !== "secret");

  const create = useMutation({
    mutationFn: (body: TriggerInput) =>
      api<TriggerWithToken>("/triggers", { method: "POST", body }),
    onSuccess: (result) => {
      void client.invalidateQueries({ queryKey: scheduleKeys.triggers });
      toast.show("Trigger created", "success");
      onCreated(result);
    },
    onError: (e) => setError(describeError(e)),
  });

  const choosePrompt = (id: string) => {
    setPromptId(id);
    const p = targets.prompts.find((x) => x.id === id);
    setProjectId((prev) => prev || p?.project_id || "");
    setRows(
      (p?.variables ?? [])
        .filter((v) => v.type !== "secret")
        .slice(0, 1)
        .map((v) => ({ variable: v.name, path: `$.client_payload.${v.name}` })),
    );
    if (!name && p) setName(`${p.name} webhook`);
  };

  return (
    <Card title="New webhook trigger">
      <form
        className="flex flex-col gap-5"
        onSubmit={(e) => {
          e.preventDefault();
          setError(null);
          create.mutate({
            name: name.trim(),
            target_type: "prompt",
            prompt_template_id: promptId || null,
            agent_profile_id: profileId || null,
            project_id: projectId || null,
            input_mapping: Object.fromEntries(
              rows
                .filter((r) => r.variable && r.path.trim())
                .map((r) => [r.variable, r.path.trim()]),
            ),
            idempotency,
            idempotency_key_path: keyPath.trim() || null,
            rate_limit_per_minute: Number(rate) || 30,
            max_payload_bytes: Math.round((Number(maxKb) || 256) * 1024),
          });
        }}
      >
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Name" htmlFor={ids.name}>
            <input
              id={ids.name}
              className={inputClass}
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
            />
          </Field>
          <Field label="Project (optional)">
            <select
              className={inputClass}
              value={projectId}
              onChange={(e) => setProjectId(e.target.value)}
            >
              <option value="">No project</option>
              {targets.projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Prompt" htmlFor={ids.prompt}>
            <select
              id={ids.prompt}
              className={inputClass}
              value={promptId}
              onChange={(e) => choosePrompt(e.target.value)}
              required
            >
              <option value="">Choose a prompt</option>
              {targets.prompts.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Agent profile" htmlFor={ids.profile}>
            <select
              id={ids.profile}
              className={inputClass}
              value={profileId}
              onChange={(e) => setProfileId(e.target.value)}
              required
            >
              <option value="">Choose an agent</option>
              {targets.agent_profiles.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </Field>
        </div>

        <fieldset className="flex flex-col gap-2">
          <legend className="text-sm font-medium text-ink-soft">Input mapping</legend>
          <p className="text-sm text-muted">
            Copy values from the JSON body into prompt inputs with a path such as{" "}
            <code>$.client_payload.branch</code> or <code>$.commits[0].id</code>. Unmapped inputs
            use the prompt's defaults. Secret inputs cannot be mapped.
          </p>
          {rows.map((row, i) => (
            <div key={i} className="flex flex-wrap items-center gap-2">
              <select
                aria-label={`Variable ${i + 1}`}
                className={cn(inputClass, "w-48")}
                value={row.variable}
                onChange={(e) =>
                  setRows(rows.map((r, j) => (j === i ? { ...r, variable: e.target.value } : r)))
                }
              >
                <option value="">Variable</option>
                {mappable.map((v) => (
                  <option key={v.name} value={v.name}>
                    {v.name}
                  </option>
                ))}
              </select>
              <span className="text-muted">from</span>
              <input
                aria-label={`JSON path ${i + 1}`}
                className={cn(inputClass, "min-w-[14rem] flex-1 font-mono")}
                value={row.path}
                spellCheck={false}
                onChange={(e) =>
                  setRows(rows.map((r, j) => (j === i ? { ...r, path: e.target.value } : r)))
                }
              />
              <IconButton
                label="Remove mapping"
                icon={<Trash2 className="size-4" />}
                onClick={() => setRows(rows.filter((_, j) => j !== i))}
              />
            </div>
          ))}
          <div>
            <Button
              size="sm"
              icon={<Plus aria-hidden className="size-3.5" />}
              disabled={!prompt || mappable.length === 0}
              onClick={() => setRows([...rows, { variable: "", path: "$." }])}
            >
              Add mapping
            </Button>
          </div>
        </fieldset>

        <div className="grid gap-4 sm:grid-cols-2">
          <Field
            label="Duplicate deliveries"
            hint={
              idempotency === "derive"
                ? "Uses the Idempotency-Key header, a delivery id header such as X-GitHub-Delivery, or a hash of the body."
                : "Deliveries without an Idempotency-Key header or delivery id are rejected."
            }
          >
            <select
              className={inputClass}
              value={idempotency}
              onChange={(e) => setIdempotency(e.target.value as "derive" | "require")}
            >
              <option value="derive">Detect automatically</option>
              <option value="require">Require a key</option>
            </select>
          </Field>
          <Field
            label="Delivery id path (optional)"
            hint="A JSON path to a unique id in the body, e.g. $.delivery.id."
          >
            <input
              className={cn(inputClass, "font-mono")}
              value={keyPath}
              onChange={(e) => setKeyPath(e.target.value)}
              spellCheck={false}
            />
          </Field>
          <Field label="Deliveries per minute">
            <input
              type="number"
              min={1}
              max={6000}
              className={inputClass}
              value={rate}
              onChange={(e) => setRate(e.target.value)}
            />
          </Field>
          <Field label="Largest body (KB)">
            <input
              type="number"
              min={1}
              max={4096}
              className={inputClass}
              value={maxKb}
              onChange={(e) => setMaxKb(e.target.value)}
            />
          </Field>
        </div>
        {error ? (
          <p role="alert" className="text-sm text-[var(--st-failed)]">
            {error}
          </p>
        ) : null}
        <div className="flex justify-end gap-2">
          <Button onClick={onCancel}>Cancel</Button>
          <Button type="submit" variant="primary" loading={create.isPending}>
            Create trigger
          </Button>
        </div>
      </form>
    </Card>
  );
}
