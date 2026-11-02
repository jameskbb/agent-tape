import { RefreshCw } from "lucide-react";

import { Badge, ErrorState, IconButton, LoadingBlock } from "@/components/ui";
import { cn } from "@/lib/cn";

import { useAdapterDetails, type AdapterDetails, type AdapterStatus } from "./api";

const STATUS_TONE: Record<AdapterStatus, "success" | "accent" | "warning" | "neutral"> = {
  ready: "success",
  installed: "accent",
  misconfigured: "warning",
  unavailable: "neutral",
};

const STATUS_TEXT: Record<AdapterStatus, string> = {
  ready: "Ready",
  installed: "Installed",
  misconfigured: "Misconfigured",
  unavailable: "Not installed",
};

export function AdapterStatusBadge({ status }: { status: AdapterStatus }) {
  return <Badge tone={STATUS_TONE[status]}>{STATUS_TEXT[status]}</Badge>;
}

export function AdapterCard({
  adapter,
  selected,
  onSelect,
}: {
  adapter: AdapterDetails;
  selected?: boolean;
  onSelect?: () => void;
}) {
  const tested =
    typeof adapter.details.tested_version === "string" ? adapter.details.tested_version : null;
  const body = (
    <>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-semibold text-ink">{adapter.display_name}</span>
        <AdapterStatusBadge status={adapter.status} />
      </div>
      <p className="text-sm text-ink-soft">{adapter.description}</p>
      <dl className="grid grid-cols-[6.5rem_minmax(0,1fr)] gap-x-2 gap-y-0.5 text-xs">
        <dt className="text-muted">Version</dt>
        <dd className="truncate">
          {adapter.version ?? "Not detected"}
          {tested && adapter.version && tested !== adapter.version ? (
            <span className="text-muted"> (tested with {tested})</span>
          ) : null}
        </dd>
        <dt className="text-muted">Executable</dt>
        <dd className="truncate font-mono" title={adapter.executable ?? undefined}>
          {adapter.executable ?? "None found"}
        </dd>
      </dl>
      {adapter.message ? <p className="text-xs text-muted">{adapter.message}</p> : null}
      <div className="flex flex-wrap gap-1">
        {adapter.capabilities.map((c) => (
          <span key={c} className="rounded bg-sunken px-1.5 py-px text-xs text-muted">
            {c.replace(/_/g, " ")}
          </span>
        ))}
      </div>
    </>
  );
  const className = cn(
    "flex flex-col gap-2 rounded-[var(--radius-panel)] border bg-surface p-4 text-left",
    selected ? "border-accent ring-1 ring-[var(--accent)]" : "border-line",
  );
  if (onSelect) {
    return (
      <button
        type="button"
        aria-pressed={selected}
        onClick={onSelect}
        className={cn(className, "transition-colors hover:border-line-strong")}
      >
        {body}
      </button>
    );
  }
  return <div className={className}>{body}</div>;
}

/** Adapter discovery results. Discovery never needs credentials and never runs a model. */
export function AdapterPanel() {
  const adapters = useAdapterDetails();
  if (adapters.isPending) return <LoadingBlock label="Discovering adapters" />;
  if (adapters.isError)
    return <ErrorState error={adapters.error} onRetry={() => void adapters.refetch()} />;
  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-2">
        <p className="text-sm text-muted">
          Found on this machine by looking for each CLI and reading its version. No credentials are
          read and no model is called.
        </p>
        <IconButton
          label="Discover again"
          icon={<RefreshCw className={cn("size-4", adapters.isFetching && "animate-spin")} />}
          onClick={() => void adapters.refetch()}
        />
      </div>
      <div className="grid gap-3 md:grid-cols-2">
        {adapters.data.map((a) => (
          <AdapterCard key={a.name} adapter={a} />
        ))}
      </div>
    </div>
  );
}
