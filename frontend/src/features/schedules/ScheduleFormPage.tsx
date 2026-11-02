import { useEffect, useId, useMemo, useState, useSyncExternalStore } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { ApiError, describeError } from "@/api/client";
import { Page } from "@/components/layout/Page";
import { Button, Card, ErrorState, LoadingBlock, useToast } from "@/components/ui";
import { cn } from "@/lib/cn";
import { text as asText } from "@/lib/format";

import {
  useSaveSchedule,
  useSchedule,
  useSchedulePreview,
  useScheduleTargets,
  useTimezones,
  type ConcurrencyPolicy,
  type MisfirePolicy,
  type Schedule,
  type ScheduleInput,
  type ScheduleKind,
  type ScheduleTargets,
  type ScheduleTiming,
  type TargetType,
  type VariableDef,
} from "./api";
import { Field, inputClass, NextRunsReel, textareaClass } from "./parts";
import {
  browserTimeZone,
  CONCURRENCY_COPY,
  CRON_PRESETS,
  INTERVAL_UNITS,
  KIND_LABELS,
  MISFIRE_COPY,
  splitInterval,
  toLocalInput,
} from "./timing";

type InputValue = string | boolean;

interface FormState {
  name: string;
  description: string;
  targetType: TargetType;
  promptId: string;
  profileId: string;
  projectId: string;
  workflowId: string;
  inputs: Record<string, InputValue>;
  kind: ScheduleKind;
  cron: string;
  intervalValue: string;
  intervalUnit: number;
  runAt: string;
  timezone: string;
  startAt: string;
  endAt: string;
  enabled: boolean;
  misfire: MisfirePolicy;
  concurrency: ConcurrencyPolicy;
  maxQueued: string;
}

function initialState(schedule?: Schedule): FormState {
  if (!schedule) {
    return {
      name: "",
      description: "",
      targetType: "prompt",
      promptId: "",
      profileId: "",
      projectId: "",
      workflowId: "",
      inputs: {},
      kind: "cron",
      cron: "0 2 * * *",
      intervalValue: "1",
      intervalUnit: 3600,
      runAt: "",
      timezone: browserTimeZone(),
      startAt: "",
      endAt: "",
      enabled: true,
      misfire: "run_once",
      concurrency: "queue",
      maxQueued: "1",
    };
  }
  const interval = splitInterval(schedule.interval_seconds);
  return {
    name: schedule.name,
    description: schedule.description ?? "",
    targetType: schedule.target_type,
    promptId: schedule.prompt_template_id ?? "",
    profileId: schedule.agent_profile_id ?? "",
    projectId: schedule.project_id ?? "",
    workflowId: schedule.workflow_id ?? "",
    inputs: Object.fromEntries(
      Object.entries(schedule.inputs).map(([k, v]) => [
        k,
        typeof v === "boolean" ? v : asText(v, ""),
      ]),
    ),
    kind: schedule.kind,
    cron: schedule.cron_expression ?? "0 2 * * *",
    intervalValue: String(interval.value),
    intervalUnit: interval.unit,
    runAt: toLocalInput(schedule.run_at_local),
    timezone: schedule.timezone,
    startAt: toLocalInput(schedule.start_at_local),
    endAt: toLocalInput(schedule.end_at_local),
    enabled: schedule.enabled,
    misfire: schedule.misfire_policy,
    concurrency: schedule.concurrency_policy,
    maxQueued: String(schedule.max_queued),
  };
}

function timingOf(f: FormState): ScheduleTiming {
  return {
    kind: f.kind,
    timezone: f.timezone.trim(),
    cron_expression: f.kind === "cron" ? f.cron.trim() : null,
    interval_seconds:
      f.kind === "interval" ? Math.round(Number(f.intervalValue) * f.intervalUnit) || null : null,
    run_at: f.kind === "one_time" ? f.runAt || null : null,
    start_at: f.kind !== "one_time" ? f.startAt || null : null,
    end_at: f.kind !== "one_time" ? f.endAt || null : null,
  };
}

const WIDE = "(min-width: 1024px)";

/** True on wide screens, where the preview sits in a side column instead of inline. */
function useWide(): boolean {
  return useSyncExternalStore(
    (notify) => {
      const mq = window.matchMedia(WIDE);
      mq.addEventListener("change", notify);
      return () => mq.removeEventListener("change", notify);
    },
    () => window.matchMedia(WIDE).matches,
    () => true,
  );
}

function useDebounced<T>(value: T, ms: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return debounced;
}

function cleanInputs(
  inputs: Record<string, InputValue>,
  vars: VariableDef[],
): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const v of vars) {
    const value = inputs[v.name];
    if (value === undefined || value === "") continue;
    out[v.name] = v.type === "number" && typeof value === "string" ? Number(value) : value;
  }
  return out;
}

export function ScheduleFormPage() {
  const { scheduleId } = useParams();
  const existing = useSchedule(scheduleId ?? "");
  const targets = useScheduleTargets();
  if (scheduleId && existing.isPending) return <LoadingBlock label="Loading schedule" />;
  if (scheduleId && existing.isError)
    return (
      <ErrorState
        error={existing.error}
        resource="schedule"
        backTo="/schedules"
        backLabel="Back to schedules"
        onRetry={() => void existing.refetch()}
      />
    );
  if (targets.isPending) return <LoadingBlock label="Loading prompts and agents" />;
  if (targets.isError)
    return <ErrorState error={targets.error} onRetry={() => void targets.refetch()} />;
  return (
    <ScheduleForm
      key={scheduleId ?? "new"}
      schedule={scheduleId ? existing.data : undefined}
      targets={targets.data}
    />
  );
}

function ScheduleForm({ schedule, targets }: { schedule?: Schedule; targets: ScheduleTargets }) {
  const [f, setF] = useState<FormState>(() => initialState(schedule));
  const set = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setF((prev) => ({ ...prev, [key]: value }));
  const navigate = useNavigate();
  const toast = useToast();
  const save = useSaveSchedule(schedule?.id);
  const zones = useTimezones();
  const ids = {
    name: useId(),
    tz: useId(),
    cron: useId(),
    runAt: useId(),
    prompt: useId(),
    profile: useId(),
  };
  const wide = useWide();
  const [error, setError] = useState<string | null>(null);
  const [issues, setIssues] = useState<string[]>([]);

  const prompt = targets.prompts.find((p) => p.id === f.promptId);
  const variables = f.targetType === "prompt" ? (prompt?.variables ?? []) : [];

  const timing = useDebounced(
    useMemo(() => timingOf(f), [f]),
    300,
  );
  const zoneKnown = !zones.data || zones.data.includes(f.timezone.trim());
  const preview = useSchedulePreview(zoneKnown && timing.timezone ? { ...timing, count: 6 } : null);

  const choosePrompt = (id: string) => {
    const p = targets.prompts.find((x) => x.id === id);
    setF((prev) => ({
      ...prev,
      promptId: id,
      inputs: {},
      projectId: prev.projectId || p?.project_id || "",
      name: prev.name || (p ? `${p.name}, scheduled` : ""),
    }));
  };

  const submit = () => {
    setError(null);
    setIssues([]);
    const body: ScheduleInput = {
      ...timingOf(f),
      name: f.name.trim(),
      description: f.description.trim() || null,
      target_type: f.targetType,
      project_id: f.projectId || null,
      prompt_template_id: f.targetType === "prompt" ? f.promptId || null : null,
      agent_profile_id: f.targetType === "prompt" ? f.profileId || null : null,
      workflow_id: f.targetType === "workflow" ? f.workflowId || null : null,
      inputs: cleanInputs(f.inputs, variables),
      enabled: f.enabled,
      misfire_policy: f.misfire,
      concurrency_policy: f.concurrency,
      max_queued: Number(f.maxQueued) || 0,
    };
    save.mutate(body, {
      onSuccess: (saved) => {
        toast.show(schedule ? "Schedule saved" : "Schedule created", "success");
        void navigate(`/schedules/${saved.id}`);
      },
      onError: (err) => {
        setError(describeError(err));
        if (err instanceof ApiError && Array.isArray(err.details.issues)) {
          setIssues(
            (err.details.issues as { field?: string; message?: string }[]).map(
              (i) => `${i.field ?? ""}: ${i.message ?? ""}`,
            ),
          );
        }
      },
    });
  };

  return (
    <Page
      title={schedule ? `Edit ${schedule.name}` : "New schedule"}
      breadcrumb={
        <Link to="/schedules" className="hover:text-ink">
          Schedules
        </Link>
      }
    >
      <form
        className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_22rem]"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <div className="flex min-w-0 flex-col gap-6">
          <Card
            title="What runs"
            description="A saved prompt, run with an agent profile. Each occurrence creates a normal run."
          >
            <div className="grid gap-4 sm:grid-cols-2">
              {targets.workflow_runs_available ? (
                <Field label="Target" className="sm:col-span-2">
                  <div className="flex gap-2" role="radiogroup" aria-label="Target">
                    {(["prompt", "workflow"] as TargetType[]).map((t) => (
                      <Segment
                        key={t}
                        active={f.targetType === t}
                        onClick={() => set("targetType", t)}
                      >
                        {t === "prompt" ? "Prompt" : "Workflow"}
                      </Segment>
                    ))}
                  </div>
                </Field>
              ) : null}
              {f.targetType === "prompt" ? (
                <>
                  <Field label="Prompt" htmlFor={ids.prompt}>
                    <select
                      id={ids.prompt}
                      className={inputClass}
                      value={f.promptId}
                      onChange={(e) => choosePrompt(e.target.value)}
                      required
                    >
                      <option value="">Choose a prompt</option>
                      {targets.prompts.map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.name} (v{p.latest_version})
                        </option>
                      ))}
                    </select>
                  </Field>
                  <Field label="Agent profile" htmlFor={ids.profile}>
                    <select
                      id={ids.profile}
                      className={inputClass}
                      value={f.profileId}
                      onChange={(e) => set("profileId", e.target.value)}
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
                </>
              ) : (
                <Field label="Workflow" className="sm:col-span-2">
                  <select
                    className={inputClass}
                    value={f.workflowId}
                    onChange={(e) => set("workflowId", e.target.value)}
                    required
                  >
                    <option value="">Choose a workflow</option>
                    {targets.workflows.map((w) => (
                      <option key={w.id} value={w.id}>
                        {w.name} (v{w.latest_version})
                      </option>
                    ))}
                  </select>
                </Field>
              )}
              <Field
                label="Project"
                hint="Runs record this project's Git state and count toward its limits."
                className="sm:col-span-2"
              >
                <select
                  className={inputClass}
                  value={f.projectId}
                  onChange={(e) => set("projectId", e.target.value)}
                >
                  <option value="">No project</option>
                  {targets.projects.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </select>
              </Field>
            </div>
            {variables.length > 0 ? (
              <fieldset className="mt-5 grid gap-4 border-t border-line pt-4 sm:grid-cols-2">
                <legend className="sr-only">Prompt inputs</legend>
                <p className="text-sm text-muted sm:col-span-2">
                  Inputs for every run. Leave a field empty to use the prompt's default.
                </p>
                {variables.map((v) => (
                  <VariableField
                    key={v.name}
                    def={v}
                    value={f.inputs[v.name]}
                    onChange={(value) => set("inputs", { ...f.inputs, [v.name]: value })}
                  />
                ))}
              </fieldset>
            ) : null}
          </Card>

          <Card
            title="When it runs"
            description="Times are entered and shown in the schedule's own time zone."
          >
            <div className="flex flex-col gap-4">
              <div className="flex flex-wrap gap-2" role="radiogroup" aria-label="Schedule type">
                {(["cron", "interval", "one_time"] as ScheduleKind[]).map((k) => (
                  <Segment key={k} active={f.kind === k} onClick={() => set("kind", k)}>
                    {KIND_LABELS[k]}
                  </Segment>
                ))}
              </div>
              <div className="grid gap-4 sm:grid-cols-2">
                <Field
                  label="Time zone"
                  htmlFor={ids.tz}
                  error={
                    zoneKnown ? null : "Use an IANA name from the list, such as Europe/London."
                  }
                  hint="Daylight saving time is handled for you."
                >
                  <input
                    id={ids.tz}
                    list={`${ids.tz}-zones`}
                    className={inputClass}
                    value={f.timezone}
                    onChange={(e) => set("timezone", e.target.value)}
                    autoComplete="off"
                    spellCheck={false}
                    required
                  />
                  <datalist id={`${ids.tz}-zones`}>
                    {(zones.data ?? []).map((z) => (
                      <option key={z} value={z} />
                    ))}
                  </datalist>
                </Field>
                {f.kind === "cron" ? (
                  <Field
                    label="Cron expression"
                    htmlFor={ids.cron}
                    hint="Minute, hour, day of month, month, day of week."
                  >
                    <input
                      id={ids.cron}
                      className={cn(inputClass, "font-mono")}
                      value={f.cron}
                      onChange={(e) => set("cron", e.target.value)}
                      spellCheck={false}
                      required
                    />
                  </Field>
                ) : f.kind === "interval" ? (
                  <Field label="Repeat every">
                    <div className="flex gap-2">
                      <input
                        type="number"
                        min={1}
                        aria-label="Interval amount"
                        className={cn(inputClass, "w-24")}
                        value={f.intervalValue}
                        onChange={(e) => set("intervalValue", e.target.value)}
                        required
                      />
                      <select
                        aria-label="Interval unit"
                        className={inputClass}
                        value={f.intervalUnit}
                        onChange={(e) => set("intervalUnit", Number(e.target.value))}
                      >
                        {INTERVAL_UNITS.map((u) => (
                          <option key={u.seconds} value={u.seconds}>
                            {u.label}
                          </option>
                        ))}
                      </select>
                    </div>
                  </Field>
                ) : (
                  <Field label="Run at" htmlFor={ids.runAt}>
                    <input
                      id={ids.runAt}
                      type="datetime-local"
                      className={inputClass}
                      value={f.runAt}
                      onChange={(e) => set("runAt", e.target.value)}
                      required
                    />
                  </Field>
                )}
              </div>
              {f.kind === "cron" ? (
                <div className="flex flex-wrap gap-1.5" aria-label="Common schedules">
                  {CRON_PRESETS.map((p) => (
                    <button
                      key={p.expression}
                      type="button"
                      onClick={() => set("cron", p.expression)}
                      className={cn(
                        "rounded-full border px-2.5 py-0.5 text-sm transition-colors",
                        f.cron.trim() === p.expression
                          ? "border-accent bg-accent-soft text-ink"
                          : "border-line text-ink-soft hover:border-line-strong",
                      )}
                    >
                      {p.label}
                    </button>
                  ))}
                </div>
              ) : null}
              {f.kind === "interval" ? (
                <p className="text-sm text-muted">
                  Intervals count elapsed time from the start time (or from when you save), so a
                  daily interval shifts by an hour on the wall clock when daylight saving changes.
                  Use cron to stay at the same local time.
                </p>
              ) : null}
              {f.kind !== "one_time" ? (
                <div className="grid gap-4 sm:grid-cols-2">
                  <Field label="Start (optional)" hint="No runs before this time.">
                    <input
                      type="datetime-local"
                      aria-label="Start"
                      className={inputClass}
                      value={f.startAt}
                      onChange={(e) => set("startAt", e.target.value)}
                    />
                  </Field>
                  <Field label="End (optional)" hint="No runs after this time.">
                    <input
                      type="datetime-local"
                      aria-label="End"
                      className={inputClass}
                      value={f.endAt}
                      onChange={(e) => set("endAt", e.target.value)}
                    />
                  </Field>
                </div>
              ) : null}
            </div>
          </Card>

          {!wide ? (
            <Card title="Next runs" description={`In ${timing.timezone || "the chosen zone"}.`}>
              <NextRunsReel
                preview={preview.data}
                loading={preview.isFetching}
                error={preview.error}
              />
            </Card>
          ) : null}
          <Card
            title="When things overlap"
            description="What the scheduler does when it was offline, or when the previous run is still going."
          >
            <div className="grid gap-5 md:grid-cols-2">
              <PolicyGroup
                legend="Missed runs"
                name="misfire"
                value={f.misfire}
                options={MISFIRE_COPY}
                onChange={(v) => set("misfire", v as MisfirePolicy)}
              />
              <div className="flex flex-col gap-3">
                <PolicyGroup
                  legend="If the previous run is still going"
                  name="concurrency"
                  value={f.concurrency}
                  options={CONCURRENCY_COPY}
                  onChange={(v) => set("concurrency", v as ConcurrencyPolicy)}
                />
                {f.concurrency === "queue" ? (
                  <Field label="Most runs waiting at once" hint="0 means never wait: skip instead.">
                    <input
                      type="number"
                      min={0}
                      max={100}
                      className={cn(inputClass, "w-24")}
                      value={f.maxQueued}
                      onChange={(e) => set("maxQueued", e.target.value)}
                    />
                  </Field>
                ) : null}
              </div>
            </div>
          </Card>

          <Card title="Name and state">
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Name" htmlFor={ids.name}>
                <input
                  id={ids.name}
                  className={inputClass}
                  value={f.name}
                  onChange={(e) => set("name", e.target.value)}
                  maxLength={200}
                  required
                />
              </Field>
              <Field label="Description (optional)">
                <input
                  className={inputClass}
                  value={f.description}
                  onChange={(e) => set("description", e.target.value)}
                />
              </Field>
              <label className="flex items-center gap-2 text-sm sm:col-span-2">
                <input
                  type="checkbox"
                  checked={f.enabled}
                  onChange={(e) => set("enabled", e.target.checked)}
                  className="size-4 accent-[var(--accent)]"
                />
                Enabled: create runs automatically when they are due
              </label>
            </div>
          </Card>

          {error ? (
            <div
              role="alert"
              className="rounded-[var(--radius-panel)] border border-[color-mix(in_srgb,var(--st-failed)_45%,transparent)] bg-[var(--st-failed-bg)] p-3 text-sm text-ink"
            >
              <p className="font-medium">{error}</p>
              {issues.length ? (
                <ul className="mt-1 list-disc pl-5">
                  {issues.map((i) => (
                    <li key={i}>{i}</li>
                  ))}
                </ul>
              ) : null}
            </div>
          ) : null}
          <div className="flex justify-end gap-2">
            <Button
              onClick={() => void navigate(schedule ? `/schedules/${schedule.id}` : "/schedules")}
            >
              Cancel
            </Button>
            <Button type="submit" variant="primary" loading={save.isPending}>
              {schedule ? "Save schedule" : "Create schedule"}
            </Button>
          </div>
        </div>

        {wide ? (
          <aside className="sticky top-6 self-start">
            <Card
              title="Next runs"
              description={`In ${timing.timezone || "the chosen zone"}, updated as you type.`}
            >
              <NextRunsReel
                preview={preview.data}
                loading={preview.isFetching}
                error={preview.error}
              />
            </Card>
          </aside>
        ) : null}
      </form>
    </Page>
  );
}

function Segment({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={active}
      onClick={onClick}
      className={cn(
        "h-8 rounded-[var(--radius-control)] border px-3 text-sm font-medium transition-colors",
        active
          ? "border-accent bg-accent-soft text-ink"
          : "border-line text-ink-soft hover:bg-sunken",
      )}
    >
      {children}
    </button>
  );
}

function PolicyGroup({
  legend,
  name,
  value,
  options,
  onChange,
}: {
  legend: string;
  name: string;
  value: string;
  options: Record<string, { label: string; detail: string }>;
  onChange: (value: string) => void;
}) {
  return (
    <fieldset className="flex flex-col gap-2">
      <legend className="mb-1 text-sm font-medium text-ink-soft">{legend}</legend>
      {Object.entries(options).map(([key, copy]) => (
        <label
          key={key}
          className={cn(
            "flex cursor-pointer gap-3 rounded-[var(--radius-control)] border px-3 py-2 transition-colors",
            value === key ? "border-accent bg-accent-soft" : "border-line hover:bg-sunken",
          )}
        >
          <input
            type="radio"
            name={name}
            value={key}
            checked={value === key}
            onChange={() => onChange(key)}
            className="mt-1 accent-[var(--accent)]"
          />
          <span>
            <span className="block font-medium">{copy.label}</span>
            <span className="block text-sm text-muted">{copy.detail}</span>
          </span>
        </label>
      ))}
    </fieldset>
  );
}

function VariableField({
  def,
  value,
  onChange,
}: {
  def: VariableDef;
  value: InputValue | undefined;
  onChange: (value: InputValue) => void;
}) {
  const id = useId();
  const label = def.label || def.name;
  const defaultText =
    def.default === undefined || def.default === null ? undefined : asText(def.default, "");
  const hint = [
    def.help,
    def.type === "secret" ? "Name of a secret reference, never the value." : null,
    defaultText !== undefined
      ? `Default: ${defaultText}`
      : def.required !== false
        ? "Required"
        : null,
  ]
    .filter(Boolean)
    .join(". ");
  if (def.type === "boolean") {
    const checked = typeof value === "boolean" ? value : def.default === true;
    return (
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={checked}
          onChange={(e) => onChange(e.target.checked)}
          className="size-4 accent-[var(--accent)]"
        />
        {label}
      </label>
    );
  }
  const text = typeof value === "string" ? value : "";
  return (
    <Field
      label={label}
      htmlFor={id}
      hint={hint}
      className={def.type === "multiline" ? "sm:col-span-2" : undefined}
    >
      {def.type === "enum" ? (
        <select
          id={id}
          className={inputClass}
          value={text}
          onChange={(e) => onChange(e.target.value)}
        >
          <option value="">{defaultText ? `Default (${defaultText})` : "Choose"}</option>
          {(def.options ?? []).map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>
      ) : def.type === "multiline" ? (
        <textarea
          id={id}
          rows={3}
          className={textareaClass}
          value={text}
          placeholder={defaultText}
          onChange={(e) => onChange(e.target.value)}
        />
      ) : (
        <input
          id={id}
          type={def.type === "number" ? "number" : "text"}
          className={inputClass}
          value={text}
          placeholder={defaultText}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
    </Field>
  );
}
