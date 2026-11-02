/** One attempt's runtime session: Herdr refs, lifecycle, attach command, honest limits. */
import { Badge, Card, CodeBlock, RelativeTime, Unavailable } from "@/components/ui";

import type { RuntimeSession } from "./api";
import { LifecycleBadge, Row } from "./parts";

function orUnavailable(value: string | number | null | undefined) {
  return value === null || value === undefined || value === "" ? <Unavailable /> : value;
}

export function SessionCard({ s }: { s: RuntimeSession }) {
  const degraded = s.capabilities.filter((c) => c.support !== "supported");
  return (
    <Card
      title={
        <span className="flex flex-wrap items-center gap-2">
          <Badge tone={s.runtime === "herdr" ? "accent" : "warning"}>
            {s.runtime === "herdr" ? "Herdr" : "Local fallback"}
          </Badge>
          Attempt {s.attempt_number ?? "?"}
        </span>
      }
      description={
        s.runtime === "herdr"
          ? `Herdr ${s.herdr_version ?? "?"} (protocol ${s.protocol ?? "?"}), session ${s.session_name ?? "default"}`
          : `Herdr was unavailable (${s.failure_category ?? "unknown"}) before the attempt started; it ran with the ${s.fallback_to ?? "local"} adapter as the profile allows.`
      }
    >
      <dl className="divide-y divide-line">
        <Row label="Machine">{s.machine}</Row>
        <Row label="Workspace">
          {orUnavailable(s.workspace_id)}
          {s.workspace_id ? (
            <span className="ml-2 text-sm text-muted">
              {s.owned_workspace ? "created by AgentTape" : "not owned (never closed by AgentTape)"}
            </span>
          ) : null}
        </Row>
        <Row label="Pane">{orUnavailable(s.pane_id)}</Row>
        <Row label="Agent">
          {s.agent_name ??
            (s.agent_kind === "demo" ? "demo agent (reported by AgentTape)" : <Unavailable />)}
          {s.agent_kind ? <span className="ml-2 text-sm text-muted">{s.agent_kind}</span> : null}
        </Row>
        <Row label="Lifecycle">
          <span className="flex flex-wrap items-center gap-2">
            <LifecycleBadge state={s.lifecycle_state} />
            <span className="text-sm text-muted">record: {s.status.replaceAll("_", " ")}</span>
            {s.failure_category ? (
              <Badge tone="danger">{s.failure_category.replaceAll("_", " ")}</Badge>
            ) : null}
          </span>
        </Row>
        <Row label="Native session">{s.native_session_ref}</Row>
        <Row label="Last sync">
          <RelativeTime iso={s.last_sync_at} />
        </Row>
      </dl>
      {s.runtime === "herdr" ? (
        <div className="mt-4 flex flex-col gap-2">
          <h3 className="text-sm font-semibold">Open in Herdr</h3>
          <p className="text-sm text-muted">{s.attach.guidance}</p>
          {s.attach.command ? (
            <CodeBlock code={s.attach.command} label="Attach command" maxHeight="6rem" />
          ) : null}
        </div>
      ) : null}
      {degraded.length ? (
        <div className="mt-4">
          <h3 className="text-sm font-semibold">What AgentTape cannot fully see here</h3>
          <ul className="mt-1 flex flex-col gap-1 text-sm text-muted">
            {degraded.map((c) => (
              <li key={c.name}>
                <span className="text-ink">{c.description}:</span> {c.note}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </Card>
  );
}
