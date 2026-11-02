/** Run-detail "Context" tab: what context was available, selected, retrieved, injected, and
 * cited, with a provenance explorer, source drawer, finding-to-source view, and review queue. */
import { Search } from "lucide-react";
import { useState, type ReactNode } from "react";

import { TERMINAL_STATUSES, type RunDetail } from "@/api/types";
import {
  Badge,
  Button,
  Card,
  DataTable,
  EmptyState,
  ErrorState,
  LoadingBlock,
  Select,
  Unavailable,
  controlClass,
  type Column,
} from "@/components/ui";
import { formatDateTime, humanize, sourceLabel, text } from "@/lib/format";

import {
  useContextPacks,
  useContextSummary,
  useEvidence,
  type ContextSummary,
  type EvidenceFilters,
  type EvidenceRow,
  type PackItem,
} from "./api";
import { FeedbackReview } from "./FeedbackReview";
import { FindingsCard } from "./FindingsCard";
import {
  QUALITY_LABEL,
  STAGE_INFO,
  STAGE_ORDER,
  freshnessLabel,
  shortHash,
  shortVersion,
} from "./labels";
import { SourceDrawer } from "./SourceDrawer";

const PAGE = 25;

function StageBadge({ stage }: { stage: keyof typeof STAGE_INFO }) {
  const info = STAGE_INFO[stage];
  return (
    <Badge tone={info.tone} title={info.help}>
      {info.label}
    </Badge>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="mt-0.5 break-words">{children}</dd>
    </div>
  );
}

function SummaryCard({ s }: { s: ContextSummary }) {
  const trigger = s.trigger;
  return (
    <Card title="Context summary" description={s.statement}>
      <dl className="grid gap-4 text-sm sm:grid-cols-2 lg:grid-cols-3">
        <Field label="Trigger">
          {sourceLabel(text(trigger.source, "unknown"))}
          {trigger.idempotency_key ? (
            <span className="block text-xs text-muted">
              Idempotency key <code>{text(trigger.idempotency_key)}</code>
            </span>
          ) : null}
          {trigger.source_ref ? (
            <span className="block text-xs text-muted">
              Reference <code>{text(trigger.source_ref)}</code>
            </span>
          ) : null}
        </Field>
        <Field label="Prompt template and version">
          {s.prompt ? (
            <>
              {s.prompt.name ?? "Inline prompt"}
              {s.prompt.version != null ? ` v${s.prompt.version}` : ""}
              <span className="block text-xs text-muted">
                Rendered hash <code>{shortHash(s.prompt.rendered_hash)}</code>
                {s.prompt.stage ? ` · ${STAGE_INFO[s.prompt.stage].label.toLowerCase()}` : ""}
              </span>
            </>
          ) : (
            <Unavailable reason="No prompt" />
          )}
        </Field>
        <Field label="Agent and model">
          {s.agent.profile ?? "No profile"}
          {s.agent.profile_version != null ? ` v${s.agent.profile_version}` : ""}
          <span className="block text-xs text-muted">
            {s.agent.adapter_type ?? "adapter unknown"} ·{" "}
            {s.agent.model ? `model ${s.agent.model}` : "model not reported"}
          </span>
        </Field>
        <Field label="Context pack">
          {s.pack ? (
            <>
              Version {s.pack.version} · {s.pack.items} items, {s.pack.excluded} excluded
              <span className="block text-xs text-muted">
                {s.pack.policy} · hash <code>{shortHash(s.pack.content_hash)}</code>
              </span>
            </>
          ) : (
            <Unavailable reason="No pack was assembled" />
          )}
        </Field>
        <Field label="Pack token estimate">
          {s.pack?.token_estimate != null ? (
            s.pack.token_estimate.toLocaleString()
          ) : (
            <Unavailable reason="Unavailable: no tokenizer or adapter count" />
          )}
        </Field>
        <Field label="Token usage reported by the adapter">
          {s.usage.status === "unavailable" ? (
            <Unavailable reason="Not reported" />
          ) : (
            <span className="inline-flex flex-wrap items-center gap-2">
              {Object.entries(s.usage.values).map(([k, v]) => (
                <span key={k} className="tabular-nums">
                  {humanize(k)} {v.toLocaleString()}
                </span>
              ))}
              {s.usage.status === "simulated" ? <Badge tone="warning">Simulated</Badge> : null}
            </span>
          )}
        </Field>
        <Field label="Live versus snapshot sources">
          {s.live_sources} live · {s.snapshot_sources} snapshot
        </Field>
        <Field label="Evidence window">
          {s.window.complete ? (
            "Complete"
          ) : (
            <span className="text-[var(--st-cancelling)]">
              Incomplete: AgentTape was offline{" "}
              {s.window.offline
                .map((o) => `from ${o.offline_from ?? "?"} to ${o.offline_to ?? "?"}`)
                .join("; ")}
              . Nothing was recorded for that window.
            </span>
          )}
        </Field>
        <Field label="Findings">{s.findings}</Field>
      </dl>
      {s.original_request ? (
        <details className="mt-4 text-sm">
          <summary className="cursor-pointer text-muted">Original request (redacted)</summary>
          <pre className="mt-2 max-h-60 overflow-auto rounded bg-sunken p-3 whitespace-pre-wrap">
            {s.original_request}
          </pre>
        </details>
      ) : null}
    </Card>
  );
}

function StageCounts({ s, onPick }: { s: ContextSummary; onPick: (stage: string) => void }) {
  return (
    <ul className="flex flex-wrap gap-2" aria-label="Context by stage">
      {s.stages
        .filter((x) => x.records > 0)
        .map((x) => (
          <li key={x.stage}>
            <button
              type="button"
              onClick={() => onPick(x.stage)}
              className="flex items-center gap-2 rounded border border-line bg-surface px-2.5 py-1.5 text-sm hover:bg-sunken"
              title={`${STAGE_INFO[x.stage].help}. Show these records.`}
            >
              <StageBadge stage={x.stage} />
              <span className="tabular-nums">
                {x.sources} source{x.sources === 1 ? "" : "s"}
              </span>
              <span className="text-xs text-muted tabular-nums">
                ({x.records} record{x.records === 1 ? "" : "s"})
              </span>
            </button>
          </li>
        ))}
    </ul>
  );
}

function Warnings({ s, onOpen }: { s: ContextSummary; onOpen: (id: string | null) => void }) {
  const groups: [string, EvidenceRow[]][] = [
    ["Freshness warnings", s.freshness_warnings],
    ["Conflicts", s.conflicts],
    ["Context gaps and unavailable tools", s.gaps],
  ];
  if (groups.every(([, rows]) => rows.length === 0)) return null;
  return (
    <Card title="Warnings">
      <div className="grid gap-4 md:grid-cols-3">
        {groups.map(([title, rows]) => (
          <section key={title} aria-label={title}>
            <h3 className="mb-1 text-sm font-semibold">{title}</h3>
            {rows.length === 0 ? (
              <p className="text-sm text-muted">None recorded.</p>
            ) : (
              <ul className="flex flex-col gap-1 text-sm">
                {rows.map((r) => (
                  <li key={r.id}>
                    <button
                      type="button"
                      className="text-left text-accent underline"
                      onClick={() => onOpen(r.source_id)}
                    >
                      {r.source_name ?? r.source_key}
                    </button>
                    {r.reason ? <span className="text-muted">: {r.reason}</span> : null}
                  </li>
                ))}
              </ul>
            )}
          </section>
        ))}
      </div>
    </Card>
  );
}

function PackCard({ runId, live }: { runId: string; live: boolean }) {
  const packs = useContextPacks(runId, live);
  if (packs.isPending) return <LoadingBlock label="Loading context pack" />;
  if (packs.isError) return <ErrorState error={packs.error} onRetry={() => void packs.refetch()} />;
  const pack = packs.data[packs.data.length - 1];
  if (!pack)
    return (
      <Card title="Context pack">
        <p className="text-sm text-muted">No context pack was assembled for this run.</p>
      </Card>
    );
  return (
    <Card
      title={`Context pack v${pack.version}`}
      description={`Assembled by AgentTape before the agent started, in this order. Immutable; hash ${shortHash(pack.content_hash)}. "Selected" items are hashed references, not copies.`}
    >
      <ol className="flex flex-col divide-y divide-line text-sm">
        {pack.items.map((item: PackItem) => (
          <li key={item.position} className="flex flex-wrap items-start gap-x-3 gap-y-1 py-2">
            <span className="w-6 text-right text-muted tabular-nums">{item.position}</span>
            <StageBadge stage={item.stage} />
            <div className="min-w-0 flex-1">
              <span className="font-medium">{item.name}</span>{" "}
              <span className="text-xs text-muted">
                {humanize(item.kind)}
                {item.version ? ` · ${shortVersion(item.version)}` : ""}
                {item.content_hash ? ` · hash ${shortHash(item.content_hash)}` : ""}
              </span>
              <p className="text-xs text-muted">Why: {item.reason}</p>
              {item.excerpt ? (
                <p className="mt-1 border-l-2 border-line pl-2 whitespace-pre-wrap">
                  {item.excerpt}
                </p>
              ) : null}
            </div>
          </li>
        ))}
      </ol>
      <h3 className="mt-4 mb-1 text-sm font-semibold">Considered but excluded</h3>
      {pack.excluded.length === 0 ? (
        <p className="text-sm text-muted">Nothing was excluded.</p>
      ) : (
        <ul className="flex flex-col gap-1 text-sm">
          {pack.excluded.map((x, i) => (
            <li key={i}>
              <Badge>Excluded</Badge> {x.name} <span className="text-muted">({x.reason})</span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function detailText(r: EvidenceRow): ReactNode {
  const parts: ReactNode[] = [];
  if (r.query) parts.push(<span key="q">query “{r.query}”</span>);
  if (r.tool)
    parts.push(
      <span key="t">
        {r.tool.server ? `${r.tool.server} · ` : ""}
        {r.tool.name} ({r.tool.status ?? "status not reported"})
      </span>,
    );
  if (r.result)
    parts.push(
      <span key="r">
        {r.result.rows != null ? `${r.result.rows} rows, ` : ""}
        hash {shortHash(r.result.hash)}
        {r.result.truncated ? ", truncated" : ""}
      </span>,
    );
  if (r.reason) parts.push(<span key="w">{r.reason}</span>);
  return parts.length ? (
    <span className="flex flex-col gap-0.5 text-xs text-ink-soft">{parts}</span>
  ) : (
    <span className="text-xs text-faint">not set</span>
  );
}

function Explorer({
  runId,
  live,
  stage,
  setStage,
  onOpen,
}: {
  runId: string;
  live: boolean;
  stage: string;
  setStage: (s: string) => void;
  onOpen: (id: string | null) => void;
}) {
  const [filters, setFilters] = useState<Omit<EvidenceFilters, "stage" | "q">>({});
  const [q, setQ] = useState("");
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const all: EvidenceFilters = { ...filters, stage: stage || undefined, q: search || undefined };
  const page = useEvidence(runId, all, offset, PAGE, live);
  const facets = page.data?.facets;
  const set = (key: keyof typeof filters, value: string) => {
    setOffset(0);
    setFilters((f) => ({ ...f, [key]: value || undefined }));
  };
  const columns: Column<EvidenceRow>[] = [
    {
      id: "ts",
      header: "When",
      cell: (r) => (
        <span className="text-xs whitespace-nowrap">{r.ts ? formatDateTime(r.ts) : ""}</span>
      ),
    },
    { id: "stage", header: "Stage", cell: (r) => <StageBadge stage={r.stage} /> },
    {
      id: "source",
      header: "Source",
      cell: (r) =>
        r.source_id ? (
          <button
            type="button"
            className="text-left text-accent underline"
            onClick={(e) => {
              e.stopPropagation();
              onOpen(r.source_id);
            }}
          >
            {r.source_name ?? r.source_key}
          </button>
        ) : (
          <span>{text(r.details.name ?? r.details.kind, "unnamed")}</span>
        ),
    },
    {
      id: "type",
      header: "Type · provider",
      cell: (r) => (
        <span className="text-xs">
          {humanize(r.source_type ?? "unknown")}
          {r.provider ? ` · ${r.provider}` : ""}
        </span>
      ),
    },
    {
      id: "fresh",
      header: "Freshness",
      cell: (r) => <span className="text-xs">{freshnessLabel(r)}</span>,
    },
    {
      id: "quality",
      header: "Evidence",
      cell: (r) => <span className="text-xs">{QUALITY_LABEL[r.quality] ?? r.quality}</span>,
    },
    { id: "detail", header: "Detail", cell: detailText },
    {
      id: "cited",
      header: "Cited in run",
      cell: (r) =>
        r.cited_in_run ? (
          <Badge tone="success">Cited</Badge>
        ) : (
          <span className="text-xs text-faint">No</span>
        ),
    },
  ];
  const select = (key: keyof typeof filters, label: string) => (
    <Select
      label={label}
      aria-label={`Filter by ${label.toLowerCase()}`}
      any="All"
      value={filters[key] ?? ""}
      onChange={(v) => set(key, v)}
      options={(facets?.[key] ?? []).map((v) => ({
        value: v,
        label: key === "quality" ? (QUALITY_LABEL[v] ?? v) : humanize(v),
      }))}
    />
  );
  return (
    <Card
      title="Provenance explorer"
      description="Every context record in chronological order: the pack AgentTape assembled, then what the agent retrieved and cited."
    >
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <Select
          label="Stage"
          aria-label="Filter by stage"
          any="All"
          value={stage}
          onChange={(v) => {
            setOffset(0);
            setStage(v);
          }}
          options={STAGE_ORDER.filter(
            (s) => !facets || facets.stage.includes(s) || s === stage,
          ).map((s) => ({ value: s, label: STAGE_INFO[s].label }))}
        />
        {select("source_type", "Type")}
        {select("provider", "Provider")}
        {select("authority", "Authority")}
        {select("freshness", "Freshness")}
        {select("quality", "Evidence")}
        {facets && facets.node_key.length > 0 ? select("node_key", "Workflow node") : null}
        <form
          className="flex items-center gap-1.5"
          role="search"
          onSubmit={(e) => {
            e.preventDefault();
            setOffset(0);
            setSearch(q.trim());
          }}
        >
          <input
            className={controlClass}
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Path, document, Jira key, table, tool…"
            aria-label="Search context sources"
          />
          <Button size="sm" type="submit" icon={<Search className="size-3.5" aria-hidden />}>
            Search
          </Button>
        </form>
      </div>
      {page.isError ? (
        <ErrorState error={page.error} onRetry={() => void page.refetch()} />
      ) : (
        <DataTable
          caption="Context evidence records"
          columns={columns}
          rows={page.data?.items ?? []}
          rowKey={(r) => r.id}
          busy={page.isFetching}
          onRowClick={(r) => r.source_id && onOpen(r.source_id)}
          empty={page.isPending ? "Loading…" : "No context records match these filters."}
          pagination={
            page.data
              ? { offset, limit: PAGE, total: page.data.total, onChange: setOffset }
              : undefined
          }
        />
      )}
    </Card>
  );
}

export function ContextTab({ detail }: { detail: RunDetail }) {
  const runId = detail.run.id;
  const live = !TERMINAL_STATUSES.has(detail.run.status);
  const summary = useContextSummary(runId, live);
  const [stage, setStage] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  if (summary.isPending) return <LoadingBlock label="Loading context" />;
  if (summary.isError)
    return <ErrorState error={summary.error} onRetry={() => void summary.refetch()} />;
  const s = summary.data;
  if (!s.pack && s.stages.every((x) => x.records === 0) && s.feedback.length === 0)
    return (
      <EmptyState
        title="No context recorded yet"
        description={
          live
            ? "The context pack is assembled when the attempt starts."
            : "This run finished before context evidence was recorded."
        }
      />
    );
  const pending = s.feedback.filter((f) => f.status === "pending").length;
  return (
    <div className="flex flex-col gap-4">
      <SummaryCard s={s} />
      <StageCounts s={s} onPick={setStage} />
      <Warnings s={s} onOpen={setOpen} />
      <div className="grid gap-4 xl:grid-cols-2">
        <PackCard runId={runId} live={live} />
        <FindingsCard runId={runId} live={live} onOpen={setOpen} />
      </div>
      <Explorer runId={runId} live={live} stage={stage} setStage={setStage} onOpen={setOpen} />
      <Card
        title={`Corrections, gaps, and lessons${pending ? ` (${pending} pending review)` : ""}`}
        description="Feedback stays tied to this run. A person classifies it, reviews the proposed destination, and approves before anything reaches future runs."
      >
        <FeedbackReview runId={runId} items={s.feedback} />
      </Card>
      <SourceDrawer runId={runId} sourceId={open} onClose={() => setOpen(null)} />
    </div>
  );
}
