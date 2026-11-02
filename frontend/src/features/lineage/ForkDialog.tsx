import { useEffect, useId, useRef, useState } from "react";

import { describeError } from "@/api/client";
import { Button } from "@/components/ui";
import { humanize, text } from "@/lib/format";

import type { DriftItem, ForkBody, ForkOptions, VariableDef } from "./api";

const FIELD =
  "rounded-[var(--radius-control)] border border-line-strong bg-surface px-2.5 py-1.5 text-base text-ink";

export function DriftList({ drift }: { drift: DriftItem[] }) {
  if (drift.length === 0) return null;
  return (
    <div
      role="status"
      className="rounded-[var(--radius-control)] bg-[var(--st-cancelling-bg)] px-3 py-2 text-sm"
    >
      <p className="font-medium text-[var(--st-cancelling)]">
        The environment differs from the original run
      </p>
      <ul className="mt-1 list-disc pl-5 text-ink-soft">
        {drift.map((d) => (
          <li key={d.field}>{d.message}</li>
        ))}
      </ul>
    </div>
  );
}

function VariableField({
  def,
  value,
  original,
  onChange,
}: {
  def: VariableDef;
  value: unknown;
  original: unknown;
  onChange: (v: unknown) => void;
}) {
  const label = def.label || humanize(def.name);
  const changed = JSON.stringify(value) !== JSON.stringify(original);
  const hint = changed ? (
    <span className="text-xs text-accent">Changed from the original</span>
  ) : def.help ? (
    <span className="text-xs text-muted">{def.help}</span>
  ) : null;
  if (def.type === "boolean") {
    return (
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={Boolean(value)}
          onChange={(e) => onChange(e.target.checked)}
        />
        <span>{label}</span>
        {hint}
      </label>
    );
  }
  let control;
  if (def.type === "enum" && def.options) {
    control = (
      <select className={FIELD} value={text(value, "")} onChange={(e) => onChange(e.target.value)}>
        {def.options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    );
  } else if (def.type === "multiline") {
    control = (
      <textarea
        className={FIELD}
        rows={3}
        value={text(value, "")}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  } else if (def.type === "number") {
    control = (
      <input
        type="number"
        className={FIELD}
        value={text(value, "")}
        onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))}
      />
    );
  } else {
    control = (
      <input className={FIELD} value={text(value, "")} onChange={(e) => onChange(e.target.value)} />
    );
  }
  return (
    <label className="flex flex-col gap-1 text-sm">
      <span className="text-muted">
        {label}
        {def.type === "secret" ? " (secret reference name; the value is never stored)" : ""}
      </span>
      {control}
      {hint}
    </label>
  );
}

/** Fork form, prefilled from the original run's snapshot. */
export function ForkDialog({
  open,
  options,
  drift,
  title,
  busy,
  onClose,
  onSubmit,
  error,
}: {
  open: boolean;
  options: ForkOptions;
  drift: DriftItem[];
  title: string;
  busy: boolean;
  onClose: () => void;
  onSubmit: (body: ForkBody) => void;
  error: unknown;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const [inputs, setInputs] = useState<Record<string, unknown>>(options.inputs);
  const [promptVersion, setPromptVersion] = useState(options.prompt_version_id ?? "");
  const [profileId, setProfileId] = useState(options.agent_profile_id ?? "");
  const [profileVersion, setProfileVersion] = useState(options.agent_profile_version_id ?? "");
  const [runTitle, setRunTitle] = useState(`Fork of ${title}`);

  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) {
      if (typeof d.showModal === "function") d.showModal();
      else d.setAttribute("open", "");
    } else if (!open && d.open) {
      if (typeof d.close === "function") d.close();
      else d.removeAttribute("open");
    }
  }, [open]);

  const profile = options.profiles.find((p) => p.id === profileId);
  const changedInputs = Object.keys(inputs).filter(
    (k) => JSON.stringify(inputs[k]) !== JSON.stringify(options.inputs[k]),
  );
  const changes =
    changedInputs.length +
    (promptVersion && promptVersion !== options.prompt_version_id ? 1 : 0) +
    (profileVersion && profileVersion !== options.agent_profile_version_id ? 1 : 0);

  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      onClose={onClose}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
      className="m-auto w-[min(94vw,36rem)] rounded-[var(--radius-panel)] border border-line bg-raised p-0 text-ink shadow-[var(--shadow-pop)] backdrop:bg-[rgb(10_16_24/0.45)]"
    >
      <form
        method="dialog"
        className="flex max-h-[85vh] flex-col gap-4 overflow-y-auto p-5"
        onSubmit={(e) => {
          e.preventDefault();
          const body: ForkBody = { title: runTitle.trim() || undefined, inputs };
          if (promptVersion && promptVersion !== options.prompt_version_id)
            body.prompt_version_id = promptVersion;
          if (profileVersion && profileVersion !== options.agent_profile_version_id) {
            body.agent_profile_id = profileId;
            body.agent_profile_version_id = profileVersion;
          }
          onSubmit(body);
        }}
      >
        <div>
          <h2 id={titleId} className="text-lg font-semibold">
            Fork this run
          </h2>
          <p className="mt-1 text-sm text-ink-soft">
            Starts a new run from the same snapshot with the changes you make here. The original run
            is not modified; the two are linked as parent and fork.
          </p>
        </div>
        <DriftList drift={drift} />
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-muted">Title</span>
          <input className={FIELD} value={runTitle} onChange={(e) => setRunTitle(e.target.value)} />
        </label>
        {options.prompt_versions.length > 0 ? (
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-muted">Prompt version ({options.prompt_name})</span>
            <select
              className={FIELD}
              value={promptVersion}
              onChange={(e) => setPromptVersion(e.target.value)}
            >
              {options.prompt_versions.map((v) => (
                <option key={v.id} value={v.id}>
                  {v.label}
                  {v.id === options.prompt_version_id ? " (original)" : ""}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_8rem]">
          <label className="flex min-w-0 flex-col gap-1 text-sm">
            <span className="text-muted">Agent profile</span>
            <select
              className={FIELD}
              value={profileId}
              onChange={(e) => {
                const next = options.profiles.find((p) => p.id === e.target.value);
                setProfileId(e.target.value);
                setProfileVersion(next?.versions[0]?.id ?? "");
              }}
            >
              {options.profiles.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-muted">Version</span>
            <select
              className={FIELD}
              value={profileVersion}
              onChange={(e) => setProfileVersion(e.target.value)}
            >
              {(profile?.versions ?? []).map((v) => (
                <option key={v.id} value={v.id}>
                  {v.label}
                  {v.id === options.agent_profile_version_id ? " (original)" : ""}
                </option>
              ))}
            </select>
          </label>
        </div>
        {options.variables.length > 0 ? (
          <fieldset className="flex flex-col gap-3">
            <legend className="mb-1 text-sm font-medium">Variables</legend>
            {options.variables.map((def) => (
              <VariableField
                key={def.name}
                def={def}
                value={inputs[def.name]}
                original={options.inputs[def.name]}
                onChange={(v) => setInputs((prev) => ({ ...prev, [def.name]: v }))}
              />
            ))}
          </fieldset>
        ) : null}
        {error ? <p className="text-sm text-[var(--st-failed)]">{describeError(error)}</p> : null}
        <div className="flex items-center justify-end gap-2">
          <span className="mr-auto text-sm text-muted">
            {changes === 0 ? "No changes yet" : `${changes} change${changes === 1 ? "" : "s"}`}
          </span>
          <Button onClick={onClose} disabled={busy}>
            Keep as is
          </Button>
          <Button type="submit" variant="primary" loading={busy} disabled={changes === 0}>
            Start fork
          </Button>
        </div>
      </form>
    </dialog>
  );
}
