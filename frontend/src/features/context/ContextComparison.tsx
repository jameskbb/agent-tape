/** Context section of the run comparison page: how two runs' supplied and retrieved context
 * differed (sources, prompt and instructions, versions, queries, MCP, gaps, citations). */
import { Badge, Card, ErrorState, LoadingBlock } from "@/components/ui";

import { useContextComparison, type ContextComparison as Cmp } from "./api";
import { plural, shortHash } from "./labels";

const CHANGE_TONE = {
  added: "success",
  removed: "danger",
  changed: "warning",
  same: "neutral",
} as const;
const CHANGE_LABEL = {
  added: "Only in B",
  removed: "Only in A",
  changed: "Stages differ",
  same: "Same",
};

function Diff({ title, a, b }: { title: string; a: string[]; b: string[] }) {
  if (a.length === 0 && b.length === 0) return null;
  return (
    <div className="text-sm">
      <h3 className="mb-1 font-semibold">{title}</h3>
      <ul className="flex flex-col gap-0.5">
        {a.map((x) => (
          <li key={`a-${x}`}>
            <Badge tone="danger">Only in A</Badge> {x}
          </li>
        ))}
        {b.map((x) => (
          <li key={`b-${x}`}>
            <Badge tone="success">Only in B</Badge> {x}
          </li>
        ))}
      </ul>
    </div>
  );
}

function Body({ c }: { c: Cmp }) {
  const changed = c.sources.filter((s) => s.change !== "same");
  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm text-muted">
        {c.same_pack
          ? "Both runs started from an identical context pack."
          : `Context packs differ: ${plural(c.counts.added ?? 0, "source")} only in B, ${c.counts.removed ?? 0} only in A, ${c.counts.changed ?? 0} with different stages.`}
      </p>
      <div className="grid gap-4 md:grid-cols-2">
        <div className="text-sm">
          <h3 className="mb-1 font-semibold">Prompt</h3>
          <p>
            A: {c.prompt.a.name ?? "none"} {c.prompt.a.version ?? ""} (
            <code>{shortHash(c.prompt.a.hash)}</code>)
          </p>
          <p>
            B: {c.prompt.b.name ?? "none"} {c.prompt.b.version ?? ""} (
            <code>{shortHash(c.prompt.b.hash)}</code>)
          </p>
          <p className="text-xs text-muted">
            {c.prompt.same ? "Rendered prompts are identical." : "Rendered prompts differ."}
          </p>
        </div>
        <div className="text-sm">
          <h3 className="mb-1 font-semibold">Instruction files and skills</h3>
          {c.instructions.length === 0 ? (
            <p className="text-muted">None recorded in either run.</p>
          ) : (
            <ul className="flex flex-col gap-0.5">
              {c.instructions.map((i) => (
                <li key={i.name}>
                  {i.name}{" "}
                  {i.changed ? (
                    <Badge tone="warning">
                      {i.a_hash && i.b_hash ? "Changed" : i.a_hash ? "Only in A" : "Only in B"}
                    </Badge>
                  ) : (
                    <span className="text-xs text-faint">same</span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
      {changed.length ? (
        <div className="text-sm">
          <h3 className="mb-1 font-semibold">Sources</h3>
          <ul className="flex flex-col gap-1" aria-label="Context source differences">
            {changed.map((s) => (
              <li key={s.key} className="flex flex-wrap items-center gap-2">
                <Badge tone={CHANGE_TONE[s.change]}>{CHANGE_LABEL[s.change]}</Badge>
                <span className="font-medium">{s.name}</span>
                <span className="text-xs text-muted">
                  A: {s.a_stages.join(", ") || "none"} · B: {s.b_stages.join(", ") || "none"}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {c.versions.length ? (
        <div className="text-sm">
          <h3 className="mb-1 font-semibold">Source versions and freshness</h3>
          <ul className="flex flex-col gap-0.5">
            {c.versions.map((v) => (
              <li key={v.key}>
                {v.name}: A {v.a.version ?? "no version"} ({v.a.freshness}) → B{" "}
                {v.b.version ?? "no version"} ({v.b.freshness})
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {c.mcp.length ? (
        <div className="text-sm">
          <h3 className="mb-1 font-semibold">MCP availability</h3>
          <ul className="flex flex-col gap-0.5">
            {c.mcp.map((m) => (
              <li key={m.tool}>
                {m.tool}: A {m.a ?? "not used"} · B {m.b ?? "not used"}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      <div className="grid gap-4 md:grid-cols-2">
        <Diff title="Queries" a={c.queries.a_only} b={c.queries.b_only} />
        <Diff title="Approved lessons injected" a={c.lessons.a_only} b={c.lessons.b_only} />
        <Diff title="Citations" a={c.citations.a_only} b={c.citations.b_only} />
        <Diff title="Conclusions" a={c.conclusions.a_only} b={c.conclusions.b_only} />
        <Diff title="Context gaps" a={c.gaps.resolved} b={c.gaps.introduced} />
      </div>
    </div>
  );
}

export function ContextComparison({ a, b }: { a: string; b: string }) {
  const q = useContextComparison(a, b);
  return (
    <Card
      aria-label="Context"
      title="Context"
      description="What each run was supplied and retrieved. Gaps only in A were resolved in B; gaps only in B were introduced."
    >
      {q.isPending ? (
        <LoadingBlock label="Comparing context" />
      ) : q.isError ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} />
      ) : (
        <Body c={q.data} />
      )}
    </Card>
  );
}
