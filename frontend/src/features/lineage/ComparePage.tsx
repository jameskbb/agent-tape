import { ArrowLeftRight } from "lucide-react";
import type { ReactNode } from "react";
import { Link, useSearchParams } from "react-router";

import { useRuns } from "@/api/hooks";
import { Page } from "@/components/layout/Page";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorState,
  LoadingBlock,
  StatusPill,
  Unavailable,
  Cost,
} from "@/components/ui";
import { sameCost } from "@/lib/cost";
import { cn } from "@/lib/cn";
import { formatBytes, formatDuration, humanize, text } from "@/lib/format";

import { ContextComparison } from "@/features/context/ContextComparison";

import { useComparison, type Comparison, type NamedDiff, type RunSide } from "./api";

const SELECT =
  "min-w-0 flex-1 rounded-[var(--radius-control)] border border-line-strong bg-surface px-2.5 py-1.5 text-base text-ink";

function RunPicker({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string | null;
  onChange: (id: string) => void;
}) {
  const runs = useRuns(
    { limit: 100, sort: "created_at", order: "desc" },
    { refetchInterval: false },
  );
  const items = runs.data?.items ?? [];
  const known = items.some((r) => r.id === value);
  return (
    <label className="flex min-w-0 flex-1 items-center gap-2 text-sm">
      <span className="w-5 shrink-0 font-semibold text-muted">{label}</span>
      <select
        aria-label={`Run ${label}`}
        className={SELECT}
        value={value ?? ""}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="">Choose a run…</option>
        {value && !known ? <option value={value}>{value.slice(0, 8)}</option> : null}
        {items.map((r) => (
          <option key={r.id} value={r.id}>
            {r.title} ({r.id.slice(0, 8)}, {r.status.replace(/_/g, " ")})
          </option>
        ))}
      </select>
    </label>
  );
}

function signed(n: number | null | undefined, fmt: (v: number) => string = String): ReactNode {
  if (n === null || n === undefined) return <Unavailable reason="n/a" />;
  if (n === 0) return <span className="text-faint">same</span>;
  return (
    <span className="tabular-nums">
      {n > 0 ? "+" : "−"}
      {fmt(Math.abs(n))}
    </span>
  );
}

function usageText(side: RunSide, key: string): ReactNode {
  if (side.usage_status === "unavailable") return <Unavailable reason="Not reported" />;
  const v = side.usage?.[key];
  return typeof v === "number" ? (
    <span className="inline-flex items-center gap-1.5 tabular-nums">
      {v.toLocaleString()}
      {side.usage_status === "simulated" ? <Badge tone="warning">Simulated</Badge> : null}
    </span>
  ) : (
    <Unavailable reason="Not reported" />
  );
}

function Row({
  label,
  a,
  b,
  diff,
  changed,
}: {
  label: string;
  a: ReactNode;
  b: ReactNode;
  diff?: ReactNode;
  changed: boolean;
}) {
  return (
    <tr
      data-changed={changed || undefined}
      className={cn("border-b border-line last:border-0", changed && "bg-accent-soft/60")}
    >
      <th scope="row" className="py-2 pr-3 pl-3 text-left text-sm font-medium text-muted">
        <span className="inline-flex items-center gap-2">
          <span
            aria-hidden
            className={cn(
              "inline-block size-1.5 rounded-full",
              changed ? "bg-accent" : "bg-transparent",
            )}
          />
          {label}
          {changed ? <span className="sr-only">(differs)</span> : null}
        </span>
      </th>
      <td className="py-2 pr-3 text-sm">{a}</td>
      <td className="py-2 pr-3 text-sm">{b}</td>
      <td className="py-2 pr-3 text-right text-sm text-ink-soft">{diff}</td>
    </tr>
  );
}

function Grid({ caption, children }: { caption: string; children: ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[36rem] table-fixed border-collapse">
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr className="border-b border-line-strong text-left text-xs text-muted">
            <th scope="col" className="w-52 py-2 pl-3 font-medium" />
            <th scope="col" className="py-2 pr-3 font-medium">
              Run A
            </th>
            <th scope="col" className="py-2 pr-3 font-medium">
              Run B
            </th>
            <th scope="col" className="w-32 py-2 pr-3 text-right font-medium">
              Difference
            </th>
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

function NamedRows({
  items,
  keyName,
  render,
}: {
  items: NamedDiff[];
  keyName: string;
  render: (item: NamedDiff, side: "a" | "b") => ReactNode;
}) {
  return (
    <>
      {items.map((item) => (
        <Row
          key={String(item[keyName])}
          label={String(item[keyName])}
          a={item.in_a ? render(item, "a") : <span className="text-faint">absent</span>}
          b={item.in_b ? render(item, "b") : <span className="text-faint">absent</span>}
          diff={!item.in_a ? "only in B" : !item.in_b ? "only in A" : item.changed ? "differs" : ""}
          changed={item.changed}
        />
      ))}
    </>
  );
}

const EVAL_TONE: Record<string, "success" | "danger" | "warning" | "neutral"> = {
  pass: "success",
  fail: "danger",
  error: "danger",
  warning: "warning",
  skipped: "neutral",
};

function value(v: unknown): string {
  if (v === null || v === undefined) return "not set";
  return typeof v === "string" ? v : JSON.stringify(v);
}

function RunHeader({ side, label }: { side: RunSide; label: string }) {
  return (
    <div className="flex min-w-0 flex-1 flex-col gap-1.5 rounded-[var(--radius-panel)] border border-line bg-surface p-4">
      <span className="text-xs font-semibold text-muted">Run {label}</span>
      <Link to={`/runs/${side.run.id}`} className="truncate font-semibold hover:text-accent">
        {side.run.title}
      </Link>
      <div className="flex flex-wrap items-center gap-2 text-sm text-muted">
        <StatusPill status={side.run.status} size="sm" />
        <code className="text-xs">{side.run.id.slice(0, 8)}</code>
        <span>{humanize(side.run.source)}</span>
      </div>
    </div>
  );
}

function relationText(relation: string | null): string | null {
  if (!relation) return null;
  const m = /^(a|b)_is_(\w+)_of_(a|b)$/.exec(relation);
  if (m) return `${m[1]?.toUpperCase()} is a ${m[2]} of ${m[3]?.toUpperCase()}`;
  return relation === "siblings" ? "A and B share a parent" : humanize(relation);
}

function ComparisonView({ cmp }: { cmp: Comparison }) {
  const { a, b, differences: d } = cmp;
  const rel = relationText(cmp.relation);
  const promptLabel = (s: RunSide) =>
    s.prompt ? `${s.prompt.name ?? "Prompt"} v${s.prompt.version ?? "?"}` : "No prompt";
  const profileLabel = (s: RunSide) =>
    s.profile ? `${s.profile.name ?? "Profile"} v${s.profile.version ?? "?"}` : "No profile";
  const changedTypes = d.event_types.filter((t) => t.delta !== 0).length;
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col items-stretch gap-3 md:flex-row md:items-center">
        <RunHeader side={a} label="A" />
        <div className="flex flex-col items-center gap-1 text-center text-xs text-muted md:w-32">
          <ArrowLeftRight aria-hidden className="size-4" />
          {rel ? <span data-testid="relation">{rel}</span> : <span>Not directly related</span>}
        </div>
        <RunHeader side={b} label="B" />
      </div>

      <Card title="Outcome and cost" padded={false}>
        <Grid caption="Outcome comparison">
          <Row
            label="Status"
            a={<StatusPill status={a.run.status} size="sm" />}
            b={<StatusPill status={b.run.status} size="sm" />}
            diff={d.status.same ? "" : "differs"}
            changed={!d.status.same}
          />
          <Row
            label="Duration"
            a={formatDuration(d.duration_ms.a) ?? <Unavailable reason="Not measured" />}
            b={formatDuration(d.duration_ms.b) ?? <Unavailable reason="Not measured" />}
            diff={signed(d.duration_ms.delta, (v) => formatDuration(v) ?? String(v))}
            changed={Boolean(d.duration_ms.delta)}
          />
          <Row
            label="Attempts"
            a={d.attempts.a}
            b={d.attempts.b}
            diff={signed(d.attempts.delta)}
            changed={d.attempts.delta !== 0}
          />
          <Row
            label="Events"
            a={d.events.a}
            b={d.events.b}
            diff={signed(d.events.delta)}
            changed={d.events.delta !== 0}
          />
          {d.usage.rows.length === 0 ? (
            <Row
              label="Token usage"
              a={<Unavailable reason="Not reported" />}
              b={<Unavailable reason="Not reported" />}
              changed={false}
            />
          ) : (
            d.usage.rows.map((r) => (
              <Row
                key={r.key}
                label={humanize(r.key)}
                a={usageText(a, r.key)}
                b={usageText(b, r.key)}
                diff={signed(r.delta, (v) => v.toLocaleString())}
                changed={Boolean(r.delta)}
              />
            ))
          )}
          <Row
            label="Cost"
            a={<Cost cost={a.cost} />}
            b={<Cost cost={b.cost} />}
            changed={!sameCost(a.cost, b.cost)}
          />
        </Grid>
      </Card>

      <Card title="Configuration" padded={false}>
        <Grid caption="Configuration comparison">
          <Row
            label="Prompt version"
            a={promptLabel(a)}
            b={promptLabel(b)}
            diff={
              !d.prompt.same_template
                ? "different prompt"
                : !d.prompt.same_version
                  ? "different version"
                  : ""
            }
            changed={!d.prompt.same_version}
          />
          <Row
            label="Rendered prompt"
            a={<span className="line-clamp-2 text-ink-soft">{a.prompt?.rendered ?? ""}</span>}
            b={<span className="line-clamp-2 text-ink-soft">{b.prompt?.rendered ?? ""}</span>}
            diff={d.prompt.same_rendered ? "" : "differs"}
            changed={!d.prompt.same_rendered}
          />
          <Row
            label="Agent profile"
            a={profileLabel(a)}
            b={profileLabel(b)}
            diff={
              !d.profile.same_profile
                ? "different profile"
                : !d.profile.same_version
                  ? "different version"
                  : ""
            }
            changed={!d.profile.same_version}
          />
          {d.profile.config.map((c) => (
            <Row
              key={`cfg-${c.key}`}
              label={`Profile: ${c.key}`}
              a={<code>{value(c.a)}</code>}
              b={<code>{value(c.b)}</code>}
              diff="differs"
              changed
            />
          ))}
          {d.inputs.length === 0 ? (
            <Row label="Variables" a="identical" b="identical" changed={false} />
          ) : (
            d.inputs.map((i) => (
              <Row
                key={`in-${i.key}`}
                label={`Variable: ${i.key}`}
                a={<span className="break-words">{value(i.a)}</span>}
                b={<span className="break-words">{value(i.b)}</span>}
                diff="differs"
                changed
              />
            ))
          )}
        </Grid>
      </Card>

      <Card
        title="Git changes"
        description="Latest attempt's changed files. Unchanged files are not listed."
        padded={false}
      >
        <Grid caption="Git change comparison">
          <Row
            label="Files changed"
            a={a.git.files}
            b={b.git.files}
            diff={signed(b.git.files - a.git.files)}
            changed={a.git.files !== b.git.files}
          />
          <Row
            label="Lines"
            a={
              <span className="tabular-nums">
                +{a.git.insertions} −{a.git.deletions}
              </span>
            }
            b={
              <span className="tabular-nums">
                +{b.git.insertions} −{b.git.deletions}
              </span>
            }
            changed={a.git.insertions !== b.git.insertions || a.git.deletions !== b.git.deletions}
          />
          <Row
            label="Protected paths"
            a={a.git.protected}
            b={b.git.protected}
            changed={a.git.protected !== b.git.protected}
          />
          <Row
            label="Starting revision"
            a={
              a.git.head ? <code>{a.git.head.slice(0, 10)}</code> : <Unavailable reason="No Git" />
            }
            b={
              b.git.head ? <code>{b.git.head.slice(0, 10)}</code> : <Unavailable reason="No Git" />
            }
            changed={a.git.head !== b.git.head}
          />
          <NamedRows
            items={d.files}
            keyName="path"
            render={(item, side) => (
              <span className="tabular-nums">
                {humanize(String(item[`${side}_change_type`]))} +{text(item[`${side}_insertions`])}{" "}
                −{text(item[`${side}_deletions`])}
              </span>
            )}
          />
        </Grid>
      </Card>

      <Card title="Evaluations" padded={false}>
        {d.evaluations.length === 0 ? (
          <p className="p-4 text-sm text-muted">Neither run was evaluated.</p>
        ) : (
          <Grid caption="Evaluation comparison">
            <NamedRows
              items={d.evaluations}
              keyName="name"
              render={(item, side) => {
                const status = String(item[`${side}_status`]);
                return <Badge tone={EVAL_TONE[status] ?? "neutral"}>{humanize(status)}</Badge>;
              }}
            />
          </Grid>
        )}
      </Card>

      <Card
        title="Events by type"
        description={
          changedTypes
            ? `${changedTypes} event type${changedTypes === 1 ? "" : "s"} differ in count.`
            : "Both runs recorded the same number of each event type."
        }
        padded={false}
      >
        <Grid caption="Event type counts">
          {d.event_types.map((t) => (
            <Row
              key={t.key}
              label={t.key}
              a={t.a}
              b={t.b}
              diff={signed(t.delta)}
              changed={t.delta !== 0}
            />
          ))}
        </Grid>
      </Card>

      <Card title="Artifacts" padded={false}>
        {d.artifacts.length === 0 ? (
          <p className="p-4 text-sm text-muted">Neither run stored artifacts.</p>
        ) : (
          <Grid caption="Artifact comparison">
            <NamedRows
              items={d.artifacts}
              keyName="name"
              render={(item, side) => formatBytes(Number(item[`${side}_size_bytes`] ?? 0))}
            />
          </Grid>
        )}
      </Card>

      <ContextComparison a={cmp.a.run.id} b={cmp.b.run.id} />
    </div>
  );
}

export function ComparePage() {
  const [params, setParams] = useSearchParams();
  const a = params.get("a");
  const b = params.get("b");
  const cmp = useComparison(a, b);
  const set = (key: "a" | "b", id: string) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (id) next.set(key, id);
        else next.delete(key);
        return next;
      },
      { replace: true },
    );
  return (
    <Page
      title="Compare runs"
      breadcrumb={
        <Link to="/runs" className="hover:text-ink">
          Runs
        </Link>
      }
      description="Two runs side by side. Rows that differ are marked; unknown values stay unavailable instead of zero."
    >
      <div className="flex flex-col gap-2 md:flex-row md:items-center">
        <RunPicker label="A" value={a} onChange={(id) => set("a", id)} />
        <Button
          variant="ghost"
          size="sm"
          icon={<ArrowLeftRight className="size-4" aria-hidden />}
          disabled={!a || !b}
          onClick={() =>
            setParams(
              (prev) => {
                const next = new URLSearchParams(prev);
                if (a && b) {
                  next.set("a", b);
                  next.set("b", a);
                }
                return next;
              },
              { replace: true },
            )
          }
        >
          Swap
        </Button>
        <RunPicker label="B" value={b} onChange={(id) => set("b", id)} />
      </div>
      {!a || !b ? (
        <EmptyState
          title="Choose two runs"
          description="Pick run A and run B above. A replay or fork compared with its original shows exactly what changed. The URL can be shared."
        />
      ) : a === b ? (
        <EmptyState
          title="Choose two different runs"
          description="Run A and run B are the same run."
        />
      ) : cmp.isPending ? (
        <LoadingBlock label="Comparing runs" />
      ) : cmp.isError ? (
        <ErrorState error={cmp.error} onRetry={() => void cmp.refetch()} />
      ) : (
        <ComparisonView cmp={cmp.data} />
      )}
    </Page>
  );
}
