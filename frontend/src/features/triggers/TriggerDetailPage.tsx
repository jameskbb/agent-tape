import { useMutation, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Power, Trash2 } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { api, describeError } from "@/api/client";
import { Page } from "@/components/layout/Page";
import {
  Badge,
  Button,
  Card,
  CodeBlock,
  ConfirmDialog,
  DataTable,
  ErrorState,
  LoadingBlock,
  RelativeTime,
  useToast,
  type Column,
} from "@/components/ui";
import { formatBytes } from "@/lib/format";
import {
  scheduleKeys,
  useDeliveries,
  useTrigger,
  type Delivery,
  type MappingTestResult,
  type Trigger,
  type TriggerWithToken,
} from "@/features/schedules/api";
import { textareaClass } from "@/features/schedules/parts";

import {
  curlExample,
  githubActionsExample,
  githubDispatchExample,
  samplePayload,
} from "./examples";
import { TokenReveal } from "./TriggersPage";

const DELIVERY_TONES: Record<Delivery["status"], "success" | "neutral" | "danger" | "warning"> = {
  accepted: "success",
  duplicate: "neutral",
  rejected: "danger",
  error: "warning",
};

const deliveryColumns: Column<Delivery>[] = [
  {
    id: "when",
    header: "Received",
    cell: (d) => <RelativeTime iso={d.received_at} className="text-ink-soft" />,
  },
  {
    id: "status",
    header: "Result",
    cell: (d) => (
      <span className="inline-flex items-center gap-2">
        <Badge tone={DELIVERY_TONES[d.status]}>{d.status}</Badge>
        <code className="text-xs text-muted">{d.http_status}</code>
      </span>
    ),
  },
  {
    id: "run",
    header: "Run",
    cell: (d) =>
      d.run_id ? (
        <Link to={`/runs/${d.run_id}`} className="text-accent hover:underline">
          <code>{d.run_id.slice(0, 8)}</code>
        </Link>
      ) : (
        <span className="text-faint">None</span>
      ),
  },
  {
    id: "detail",
    header: "Detail",
    cell: (d) => (
      <span className="text-sm text-muted">
        {d.error ??
          [
            d.metadata.headers?.["x-github-event"]
              ? `GitHub ${d.metadata.headers["x-github-event"]}`
              : null,
            d.metadata.idempotency_source ? `key from ${d.metadata.idempotency_source}` : null,
          ]
            .filter(Boolean)
            .join(", ")}
      </span>
    ),
  },
  { id: "size", header: "Size", align: "right", cell: (d) => formatBytes(d.payload_bytes) },
];

export function TriggerDetailPage() {
  const { triggerId = "" } = useParams();
  const trigger = useTrigger(triggerId);
  if (trigger.isPending) return <LoadingBlock label="Loading trigger" />;
  if (trigger.isError)
    return (
      <ErrorState
        error={trigger.error}
        resource="webhook trigger"
        backTo="/settings/triggers"
        backLabel="Back to webhook triggers"
        onRetry={() => void trigger.refetch()}
      />
    );
  return <TriggerDetail trigger={trigger.data} />;
}

function TriggerDetail({ trigger: t }: { trigger: Trigger }) {
  const client = useQueryClient();
  const toast = useToast();
  const navigate = useNavigate();
  const [offset, setOffset] = useState(0);
  const deliveries = useDeliveries(t.id, offset);
  const [dialog, setDialog] = useState<"rotate" | "delete" | "query" | null>(null);
  const [rotated, setRotated] = useState<TriggerWithToken | null>(null);
  const [sample, setSample] = useState(() =>
    JSON.stringify(samplePayload(t.input_mapping), null, 2),
  );
  const [test, setTest] = useState<MappingTestResult | string | null>(null);
  const url = `${window.location.origin}${t.webhook_path}`;
  const refresh = () => void client.invalidateQueries({ queryKey: scheduleKeys.triggers });

  const toggle = useMutation({
    mutationFn: () =>
      api<Trigger>(`/triggers/${t.id}`, { method: "PATCH", body: { enabled: !t.enabled } }),
    onSuccess: () => {
      refresh();
      toast.show(t.enabled ? "Trigger disabled" : "Trigger enabled", "success");
    },
    onError: (e) => toast.show(describeError(e), "error"),
  });
  const rotate = useMutation({
    mutationFn: () => api<TriggerWithToken>(`/triggers/${t.id}/rotate-token`, { method: "POST" }),
    onSuccess: (result) => {
      refresh();
      setDialog(null);
      setRotated(result);
    },
    onError: (e) => toast.show(describeError(e), "error"),
  });
  const queryToken = useMutation({
    mutationFn: (allow: boolean) =>
      api<Trigger>(`/triggers/${t.id}`, {
        method: "PATCH",
        body: { allow_query_token: allow },
      }),
    onSuccess: (_r, allow) => {
      refresh();
      setDialog(null);
      toast.show(allow ? "URL tokens allowed" : "URL tokens refused", "success");
    },
    onError: (e) => toast.show(describeError(e), "error"),
  });
  const remove = useMutation({
    mutationFn: () => api<unknown>(`/triggers/${t.id}`, { method: "DELETE" }),
    onSuccess: () => {
      refresh();
      toast.show("Trigger deleted", "success");
      void navigate("/settings/triggers");
    },
    onError: (e) => toast.show(describeError(e), "error"),
  });
  const runTest = useMutation({
    mutationFn: (payload: unknown) =>
      api<MappingTestResult>(`/triggers/${t.id}/test-mapping`, {
        method: "POST",
        body: { payload },
      }),
    onSuccess: setTest,
    onError: (e) => setTest(describeError(e)),
  });

  return (
    <Page
      title={t.name}
      breadcrumb={
        <Link to="/settings/triggers" className="hover:text-ink">
          Webhook triggers
        </Link>
      }
      description={`Starts "${t.target_name ?? "a deleted prompt"}"${t.agent_profile_name ? ` with ${t.agent_profile_name}` : ""} for every accepted delivery.`}
      actions={
        <>
          <Button
            icon={<Power aria-hidden className="size-4" />}
            loading={toggle.isPending}
            onClick={() => toggle.mutate()}
          >
            {t.enabled ? "Disable" : "Enable"}
          </Button>
          <Button
            icon={<KeyRound aria-hidden className="size-4" />}
            onClick={() => setDialog("rotate")}
          >
            Rotate token
          </Button>
          <Button
            variant="danger"
            icon={<Trash2 aria-hidden className="size-4" />}
            onClick={() => setDialog("delete")}
          >
            Delete
          </Button>
        </>
      }
    >
      {rotated ? <TokenReveal created={rotated} onDone={() => setRotated(null)} /> : null}
      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Endpoint">
          <dl className="flex flex-col gap-3 text-sm">
            <div>
              <dt className="text-xs text-muted">State</dt>
              <dd>
                {t.enabled ? (
                  <Badge tone="success">Enabled</Badge>
                ) : (
                  <Badge>Disabled: deliveries get 403</Badge>
                )}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted">URL (POST, JSON body)</dt>
              <dd className="break-all">
                <code>{url}</code>
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted">Token</dt>
              <dd>
                <code>{t.token_prefix}…</code> sent as <code>Authorization: Bearer</code> or{" "}
                <code>X-AgentTape-Token</code>. Stored only as a hash.
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted">Token in the URL</dt>
              <dd className="flex flex-col items-start gap-1.5">
                {t.config.allow_query_token ? (
                  <Badge tone="warning">Allowed: ?token= is accepted</Badge>
                ) : (
                  <Badge>Refused (recommended)</Badge>
                )}
                <span className="text-muted">
                  A token in the URL ends up in sender, proxy, and server logs. Allow it only for a
                  sender that cannot set headers, and rotate the token if a log leaks.
                </span>
                <Button
                  size="sm"
                  variant={t.config.allow_query_token ? "secondary" : "danger"}
                  loading={queryToken.isPending}
                  onClick={() => setDialog("query")}
                >
                  {t.config.allow_query_token ? "Refuse URL tokens" : "Allow URL tokens"}
                </Button>
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted">Limits</dt>
              <dd>
                {t.rate_limit_per_minute} deliveries per minute, bodies up to{" "}
                {formatBytes(t.max_payload_bytes)}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted">Duplicate deliveries</dt>
              <dd>
                {t.config.idempotency === "require"
                  ? "An Idempotency-Key header or delivery id is required."
                  : "Detected from Idempotency-Key, a delivery id header such as X-GitHub-Delivery"}
                {t.idempotency_key_path ? `, the body path ${t.idempotency_key_path}` : ""}
                {t.config.idempotency === "require" ? "" : ", or a hash of the body."}
              </dd>
            </div>
          </dl>
        </Card>
        <Card
          title="Input mapping"
          description="Values copied from each delivery's JSON body into prompt inputs."
        >
          {Object.keys(t.input_mapping).length === 0 ? (
            <p className="text-sm text-muted">
              No mapping: every run uses the prompt's default inputs.
            </p>
          ) : (
            <ul className="flex flex-col gap-1.5 text-sm">
              {Object.entries(t.input_mapping).map(([variable, path]) => (
                <li key={variable} className="flex flex-wrap items-center gap-2">
                  <code className="rounded bg-sunken px-1.5">{variable}</code>
                  <span className="text-muted">from</span>
                  <code className="rounded bg-sunken px-1.5">{path}</code>
                </li>
              ))}
            </ul>
          )}
          <form
            className="mt-4 flex flex-col gap-2 border-t border-line pt-4"
            onSubmit={(e) => {
              e.preventDefault();
              try {
                runTest.mutate(JSON.parse(sample) as unknown);
              } catch {
                setTest("The sample is not valid JSON.");
              }
            }}
          >
            <label className="text-sm font-medium text-ink-soft" htmlFor={`sample-${t.id}`}>
              Try the mapping on a sample body
            </label>
            <textarea
              id={`sample-${t.id}`}
              rows={5}
              className={`${textareaClass} font-mono text-sm`}
              value={sample}
              onChange={(e) => setSample(e.target.value)}
              spellCheck={false}
            />
            <div>
              <Button type="submit" size="sm" loading={runTest.isPending}>
                Test mapping
              </Button>
            </div>
            {typeof test === "string" ? (
              <p className="text-sm text-[var(--st-failed)]">{test}</p>
            ) : test ? (
              <div className="text-sm">
                <CodeBlock code={JSON.stringify(test.inputs, null, 2)} label="Run inputs" />
                {test.missing.length ? (
                  <p className="mt-1 text-[var(--st-failed)]">
                    Missing required inputs: {test.missing.join(", ")}. A delivery like this would
                    be rejected (422).
                  </p>
                ) : null}
                {test.errors.map((err) => (
                  <p key={err} className="mt-1 text-[var(--st-failed)]">
                    {err}
                  </p>
                ))}
              </div>
            ) : null}
          </form>
        </Card>
      </div>

      <Card
        title="Examples"
        description="Replace $AGENTTAPE_WEBHOOK_TOKEN with the token you stored when you created the trigger."
      >
        <div className="flex flex-col gap-3">
          <CodeBlock code={curlExample(url, t.input_mapping)} label="curl" />
          <CodeBlock
            code={githubActionsExample(url, t.input_mapping)}
            label="GitHub Actions step"
          />
          <div className="text-sm text-muted">
            <p>
              For GitHub, prefer the Actions step above: it sends the token in a header from a
              repository secret. A GitHub repository webhook cannot set custom headers, so it only
              works if you allow tokens in the URL (see Endpoint), with payload URL{" "}
              <code className="break-all">{url}?token=…</code>, content type{" "}
              <code>application/json</code>, and the "Repository dispatches" event. Each delivery's{" "}
              <code>X-GitHub-Delivery</code> id prevents duplicates. Either way, send an event with:
            </p>
          </div>
          <CodeBlock
            code={githubDispatchExample(t.input_mapping)}
            label="Send a repository_dispatch event"
          />
        </div>
      </Card>

      <Card
        title="Delivery log"
        description="Every authenticated delivery and its result. Unauthenticated requests are refused and not stored."
      >
        {deliveries.isPending ? (
          <LoadingBlock />
        ) : deliveries.isError ? (
          <ErrorState error={deliveries.error} onRetry={() => void deliveries.refetch()} />
        ) : (
          <DataTable
            caption="Deliveries"
            columns={deliveryColumns}
            rows={deliveries.data.items}
            rowKey={(d) => d.id}
            empty="No deliveries yet. Send one with the curl example above."
            pagination={{ offset, limit: 20, total: deliveries.data.total, onChange: setOffset }}
          />
        )}
      </Card>

      <ConfirmDialog
        open={dialog === "rotate"}
        title="Rotate the token?"
        description="The current token stops working immediately. Update every sender with the new token."
        confirmLabel="Rotate token"
        busy={rotate.isPending}
        onClose={() => setDialog(null)}
        onConfirm={() => rotate.mutate()}
      />
      <ConfirmDialog
        open={dialog === "query"}
        title={
          t.config.allow_query_token ? "Refuse tokens in the URL?" : "Allow tokens in the URL?"
        }
        description={
          t.config.allow_query_token
            ? "Senders that put ?token= in the URL will get 401. Header tokens keep working."
            : "The token will be accepted as ?token= in the URL. URLs are written to sender, proxy, and server logs, so anyone with those logs can start runs. Prefer the Authorization header."
        }
        confirmLabel={t.config.allow_query_token ? "Refuse URL tokens" : "Allow URL tokens"}
        tone={t.config.allow_query_token ? "primary" : "danger"}
        busy={queryToken.isPending}
        onClose={() => setDialog(null)}
        onConfirm={() => queryToken.mutate(!t.config.allow_query_token)}
      />
      <ConfirmDialog
        open={dialog === "delete"}
        title="Delete this trigger?"
        description="Deliveries to its URL will be refused. Runs it already started stay in Runs."
        confirmLabel="Delete trigger"
        tone="danger"
        busy={remove.isPending}
        onClose={() => setDialog(null)}
        onConfirm={() => remove.mutate()}
      />
    </Page>
  );
}
