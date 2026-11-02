import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";

import { api, describeError } from "@/api/client";
import { useAdapters } from "@/api/hooks";
import { Page } from "@/components/layout/Page";
import { Button, Card, ErrorState, LoadingBlock, useToast } from "@/components/ui";
import { cn } from "@/lib/cn";
import {
  scheduleKeys,
  useLimits,
  type LimitsOut,
  type LimitsSettings,
} from "@/features/schedules/api";
import { Field, inputClass } from "@/features/schedules/parts";

function toText(value: number | null | undefined): string {
  return value === null || value === undefined ? "" : String(value);
}

function toNumber(text: string): number | null {
  const n = Number(text);
  return text.trim() === "" || !Number.isFinite(n) || n <= 0 ? null : n;
}

export function LimitsPage() {
  const limits = useLimits();
  return (
    <Page
      title="Limits"
      breadcrumb={
        <Link to="/settings" className="hover:text-ink">
          Settings
        </Link>
      }
      description="How many runs may execute at once, and default budgets every run starts with. Projects, agent profiles, and individual runs can set tighter budgets."
    >
      {limits.isPending ? (
        <LoadingBlock label="Loading limits" />
      ) : limits.isError ? (
        <ErrorState error={limits.error} onRetry={() => void limits.refetch()} />
      ) : (
        <LimitsForm data={limits.data} />
      )}
    </Page>
  );
}

function LimitsForm({ data }: { data: LimitsOut }) {
  const adapters = useAdapters();
  const client = useQueryClient();
  const toast = useToast();
  const [maxRuns, setMaxRuns] = useState(toText(data.settings.max_concurrent_runs));
  const [perAdapter, setPerAdapter] = useState<Record<string, string>>(
    Object.fromEntries(Object.entries(data.settings.per_adapter).map(([k, v]) => [k, String(v)])),
  );
  const [budget, setBudget] = useState<Record<string, string>>(
    Object.fromEntries(
      Object.entries(data.settings.default_budget).map(([k, v]) => [k, String(v)]),
    ),
  );
  const [error, setError] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: (body: LimitsSettings) =>
      api<LimitsOut>("/settings/limits", { method: "PUT", body }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: scheduleKeys.limits });
      toast.show("Limits saved", "success");
    },
    onError: (e) => setError(describeError(e)),
  });
  const adapterNames = [
    ...new Set([...(adapters.data ?? []).map((a) => a.name), ...Object.keys(perAdapter)]),
  ].sort();
  const warnRatio = budget.warn_ratio ?? "";

  return (
    <form
      className="flex flex-col gap-6"
      onSubmit={(e) => {
        e.preventDefault();
        setError(null);
        const per: Record<string, number> = {};
        for (const [k, v] of Object.entries(perAdapter)) {
          const n = toNumber(v);
          if (n !== null) per[k] = Math.round(n);
        }
        const defaults: Record<string, number> = {};
        for (const [k, v] of Object.entries(budget)) {
          const n = toNumber(v);
          if (n !== null) defaults[k] = n;
        }
        const max = toNumber(maxRuns);
        save.mutate({
          max_concurrent_runs: max === null ? null : Math.round(max),
          per_adapter: per,
          default_budget: defaults,
        });
      }}
    >
      <Card
        title="Runs at the same time"
        description="A run that would exceed a limit waits in the queue and starts when a slot frees. Runs waiting for approval or a retry do not take a slot. Leave a field empty for no limit."
      >
        <div className="grid gap-4 sm:grid-cols-2">
          <Field
            label="All runs"
            hint={`${data.active_runs} running now. Each worker also runs at most ${data.worker_defaults.worker_concurrency ?? "?"} at once.`}
          >
            <input
              type="number"
              min={1}
              className={inputClass}
              value={maxRuns}
              onChange={(e) => setMaxRuns(e.target.value)}
              placeholder="No limit"
            />
          </Field>
        </div>
        <fieldset className="mt-5 flex flex-col gap-2 border-t border-line pt-4">
          <legend className="text-sm font-medium text-ink-soft">Per adapter</legend>
          <div className="grid gap-3 sm:grid-cols-2">
            {adapterNames.map((name) => (
              <label
                key={name}
                className="flex items-center justify-between gap-3 text-sm whitespace-nowrap"
              >
                <span>
                  <code>{name}</code>
                  <span className="ml-2 text-muted">
                    {data.active_by_adapter[name] ?? 0} running
                  </span>
                </span>
                <input
                  type="number"
                  min={1}
                  aria-label={`Limit for ${name}`}
                  className={cn(inputClass, "w-28")}
                  value={perAdapter[name] ?? ""}
                  placeholder="No limit"
                  onChange={(e) => setPerAdapter({ ...perAdapter, [name]: e.target.value })}
                />
              </label>
            ))}
          </div>
          <p className="text-sm text-muted">
            Per-project limits are set in each project's settings (max concurrent runs); workflows
            carry their own limit.
          </p>
        </fieldset>
      </Card>

      <Card
        title="Default budgets"
        description="Applied to every run unless its project, agent profile, or the run itself sets a different value. Token and cost budgets only count usage an agent actually reports; simulated Demo Agent numbers never count."
      >
        <div className="grid gap-4 sm:grid-cols-2">
          {data.budget_keys.map((k) => (
            <Field key={k.key} label={`${k.label} (${k.unit})`} hint={k.description}>
              <input
                type="number"
                min={0}
                step="any"
                className={inputClass}
                value={budget[k.key] ?? ""}
                placeholder="No limit"
                onChange={(e) => setBudget({ ...budget, [k.key]: e.target.value })}
              />
            </Field>
          ))}
          <Field
            label="Warn at (share of a limit)"
            hint="A warning event is recorded when a run crosses this share of a budget. Default 0.8."
          >
            <input
              type="number"
              min={0.05}
              max={0.99}
              step={0.05}
              className={inputClass}
              value={warnRatio}
              placeholder="0.8"
              onChange={(e) => setBudget({ ...budget, warn_ratio: e.target.value })}
            />
          </Field>
        </div>
      </Card>

      <Card
        title="Timeouts and retries"
        description="Server defaults. Agent profiles and projects override them per run."
      >
        <dl className="grid gap-3 text-sm sm:grid-cols-3">
          <Default
            label="Wall-clock timeout"
            value={`${data.worker_defaults.default_timeout_seconds ?? "?"} s`}
          />
          <Default
            label="Idle timeout"
            value={`${data.worker_defaults.default_idle_timeout_seconds ?? "?"} s`}
          />
          <Default
            label="Attempts"
            value={String(data.worker_defaults.default_max_attempts ?? "?")}
          />
          <Default
            label="Retry back-off"
            value={`${data.worker_defaults.retry_backoff_base_seconds ?? "?"} s, doubling, at most ${data.worker_defaults.retry_backoff_max_seconds ?? "?"} s`}
          />
        </dl>
      </Card>

      {error ? (
        <p role="alert" className="text-sm text-[var(--st-failed)]">
          {error}
        </p>
      ) : null}
      <div className="flex justify-end">
        <Button type="submit" variant="primary" loading={save.isPending}>
          Save limits
        </Button>
      </div>
    </form>
  );
}

function Default({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="mt-0.5 text-ink">{value}</dd>
    </div>
  );
}
