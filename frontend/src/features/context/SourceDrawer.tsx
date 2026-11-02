/** Side drawer with a source's safe locator, freshness, access history, and linked findings. */
import { Link } from "react-router";

import { Badge, Drawer, ErrorState, LoadingBlock, Unavailable } from "@/components/ui";
import { formatDateTime, humanize } from "@/lib/format";

import { useSourceDetail } from "./api";
import { QUALITY_LABEL, STAGE_INFO, freshnessLabel, shortHash } from "./labels";

export function SourceDrawer({
  runId,
  sourceId,
  onClose,
}: {
  runId: string;
  sourceId: string | null;
  onClose: () => void;
}) {
  const detail = useSourceDetail(runId, sourceId);
  const d = detail.data;
  return (
    <Drawer
      open={sourceId !== null}
      eyebrow="Context source"
      title={d?.source.name ?? "Loading…"}
      closeLabel="Close source details"
      onClose={onClose}
    >
      {detail.isPending ? (
        <LoadingBlock label="Loading source" />
      ) : detail.isError ? (
        <ErrorState error={detail.error} onRetry={() => void detail.refetch()} />
      ) : d ? (
        <div className="flex flex-col gap-5 text-sm">
          <dl className="grid grid-cols-[8rem_1fr] gap-x-3 gap-y-1.5">
            <dt className="text-muted">Type</dt>
            <dd>{humanize(d.source.type)}</dd>
            <dt className="text-muted">Provider</dt>
            <dd>{d.source.provider ?? <Unavailable reason="Not reported" />}</dd>
            <dt className="text-muted">Locator</dt>
            <dd className="break-all">
              {d.source.locator ? <code>{d.source.locator}</code> : <Unavailable reason="None" />}
            </dd>
            <dt className="text-muted">Authority</dt>
            <dd>{humanize(d.source.authority)}</dd>
            <dt className="text-muted">Freshness</dt>
            <dd>
              {freshnessLabel({
                freshness: d.freshness,
                live: d.source.live,
                synced_at: d.source.synced_at,
                version: d.source.version,
              })}
            </dd>
            <dt className="text-muted">Retention</dt>
            <dd>{humanize(d.source.retention)}</dd>
            <dt className="text-muted">Other runs</dt>
            <dd>
              {d.other_runs} other run{d.other_runs === 1 ? "" : "s"} recorded this source
            </dd>
          </dl>
          <div className="flex flex-wrap gap-2">
            {d.file_path ? (
              <Link
                className="text-accent underline"
                to={`/runs/${runId}?tab=files&path=${encodeURIComponent(d.file_path)}`}
              >
                File evidence for {d.file_path}
              </Link>
            ) : null}
            {d.access.some((a) => a.event_id) ? (
              <Link className="text-accent underline" to={`/runs/${runId}?tab=timeline`}>
                Open the timeline
              </Link>
            ) : null}
          </div>

          <section aria-label="Access history">
            <h3 className="mb-2 font-semibold">Access history ({d.access.length})</h3>
            <ol className="flex flex-col gap-2">
              {d.access.map((a) => (
                <li key={a.id} className="rounded border border-line p-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge tone={STAGE_INFO[a.stage].tone} title={STAGE_INFO[a.stage].help}>
                      {STAGE_INFO[a.stage].label}
                    </Badge>
                    <span className="text-xs text-muted">
                      {a.ts ? formatDateTime(a.ts) : ""} · {QUALITY_LABEL[a.quality] ?? a.quality}
                    </span>
                  </div>
                  {a.reason ? <p className="mt-1">{a.reason}</p> : null}
                  {a.query ? (
                    <p className="mt-1">
                      Query: <code className="break-all">{a.query}</code>
                    </p>
                  ) : null}
                  {a.tool ? (
                    <p className="mt-1 text-xs text-muted">
                      Tool {a.tool.server ? `${a.tool.server} · ` : ""}
                      {a.tool.name}: {a.tool.status ?? "status not reported"}
                    </p>
                  ) : null}
                  {a.result ? (
                    <p className="mt-1 text-xs text-muted">
                      Result hash <code>{shortHash(a.result.hash)}</code>
                      {a.result.rows != null ? `, ${a.result.rows} rows` : ""}
                      {a.result.truncated ? ", truncated" : ""}
                    </p>
                  ) : null}
                </li>
              ))}
            </ol>
          </section>

          <section aria-label="Linked findings">
            <h3 className="mb-2 font-semibold">Linked findings</h3>
            {d.links.length === 0 ? (
              <p className="text-muted">No finding in this run links to this source.</p>
            ) : (
              <ul className="flex flex-col gap-2">
                {d.links.map((l) => (
                  <li key={`${l.subject_key}-${l.relation}`}>
                    <Badge
                      tone={
                        l.relation === "supports"
                          ? "success"
                          : l.relation === "conflicts"
                            ? "danger"
                            : "warning"
                      }
                    >
                      {humanize(l.relation)}
                    </Badge>{" "}
                    {l.subject_text ?? l.subject_key}{" "}
                    <span className="text-xs text-muted">({humanize(l.origin)})</span>{" "}
                    <Link
                      to={`/runs/${runId}?tab=findings&finding=${encodeURIComponent(l.subject_key)}`}
                      className="text-accent hover:underline"
                    >
                      Open on Findings tab
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section aria-label="Stored excerpts">
            <h3 className="mb-2 font-semibold">Stored excerpts</h3>
            {d.excerpts.length === 0 ? (
              <p className="text-muted">
                None stored. AgentTape keeps references and hashes by default, not copies.
              </p>
            ) : (
              d.excerpts.map((e) => (
                <blockquote
                  key={e.pack_version}
                  className="border-l-2 border-line pl-3 whitespace-pre-wrap"
                >
                  {e.text}
                </blockquote>
              ))
            )}
          </section>
        </div>
      ) : null}
    </Drawer>
  );
}
