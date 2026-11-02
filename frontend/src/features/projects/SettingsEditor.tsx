import {
  CheckboxField,
  CommandListEditor,
  ListEditor,
  TextAreaField,
  TextField,
} from "@/components/forms";

import type { ProjectSettings } from "./api";

export interface SettingsForm {
  timeout_seconds: string;
  idle_timeout_seconds: string;
  max_concurrent_runs: string;
  validation_commands: string[][];
  protected_paths: string[];
  env_allowlist: string[];
  artifact_rules: string;
  git_isolation: boolean;
}

const num = (v: unknown) => (typeof v === "number" ? String(v) : "");

export function settingsToForm(s: ProjectSettings): SettingsForm {
  return {
    timeout_seconds: num(s.timeout_seconds),
    idle_timeout_seconds: num(s.idle_timeout_seconds),
    max_concurrent_runs: num(s.max_concurrent_runs),
    validation_commands: (s.validation_commands ?? []).map((c) => [...c]),
    protected_paths: [...(s.protected_paths ?? [])],
    env_allowlist: [...(s.env_allowlist ?? [])],
    artifact_rules:
      s.artifact_rules && Object.keys(s.artifact_rules).length
        ? JSON.stringify(s.artifact_rules, null, 2)
        : "",
    git_isolation: s.git_isolation === true,
  };
}

/** Build settings from the form; unknown keys (added by other features) are preserved. */
export function formToSettings(
  form: SettingsForm,
  base: ProjectSettings = {},
): { settings: ProjectSettings; errors: Partial<Record<keyof SettingsForm, string>> } {
  const errors: Partial<Record<keyof SettingsForm, string>> = {};
  const positive = (key: keyof SettingsForm, raw: string, integer = false): number | null => {
    if (!raw.trim()) return null;
    const n = Number(raw);
    if (!Number.isFinite(n) || n <= 0 || (integer && !Number.isInteger(n))) {
      errors[key] = integer ? "Must be a whole number of 1 or more" : "Must be a positive number";
      return null;
    }
    return n;
  };
  let rules: Record<string, unknown> = {};
  if (form.artifact_rules.trim()) {
    try {
      const parsed: unknown = JSON.parse(form.artifact_rules);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        rules = parsed as Record<string, unknown>;
      } else errors.artifact_rules = "Must be a JSON object";
    } catch {
      errors.artifact_rules = "Not valid JSON";
    }
  }
  return {
    settings: {
      ...base,
      timeout_seconds: positive("timeout_seconds", form.timeout_seconds),
      idle_timeout_seconds: positive("idle_timeout_seconds", form.idle_timeout_seconds),
      max_concurrent_runs: positive("max_concurrent_runs", form.max_concurrent_runs, true),
      validation_commands: form.validation_commands.filter((c) => c.some((a) => a.trim())),
      protected_paths: form.protected_paths.map((p) => p.trim()).filter(Boolean),
      env_allowlist: form.env_allowlist.map((p) => p.trim()).filter(Boolean),
      artifact_rules: rules,
      git_isolation: form.git_isolation,
    },
    errors,
  };
}

export function SettingsEditor({
  form,
  onChange,
  errors = {},
}: {
  form: SettingsForm;
  onChange: (form: SettingsForm) => void;
  errors?: Partial<Record<keyof SettingsForm, string>>;
}) {
  const set = <K extends keyof SettingsForm>(key: K, value: SettingsForm[K]) =>
    onChange({ ...form, [key]: value });
  return (
    <div className="grid gap-4 sm:grid-cols-3">
      <TextField
        label="Timeout (seconds)"
        inputMode="decimal"
        value={form.timeout_seconds}
        onChange={(v) => set("timeout_seconds", v)}
        error={errors.timeout_seconds}
        help="Default wall-clock limit for runs here."
      />
      <TextField
        label="Idle timeout (seconds)"
        inputMode="decimal"
        value={form.idle_timeout_seconds}
        onChange={(v) => set("idle_timeout_seconds", v)}
        error={errors.idle_timeout_seconds}
      />
      <TextField
        label="Max concurrent runs"
        inputMode="numeric"
        value={form.max_concurrent_runs}
        onChange={(v) => set("max_concurrent_runs", v)}
        error={errors.max_concurrent_runs}
      />
      <div className="sm:col-span-3">
        <CommandListEditor
          label="Validation commands"
          help="Run after an agent changes files, as argument arrays (never a shell string)."
          commands={form.validation_commands}
          onChange={(v) => set("validation_commands", v)}
        />
      </div>
      <div className="sm:col-span-3 grid gap-4 sm:grid-cols-2">
        <ListEditor
          label="Protected paths"
          itemLabel="Path"
          addLabel="Add path"
          placeholder=".github/**"
          values={form.protected_paths}
          onChange={(v) => set("protected_paths", v)}
          help="Glob patterns. Writing to them requires approval."
        />
        <ListEditor
          label="Environment allowlist"
          itemLabel="Variable"
          addLabel="Add variable"
          placeholder="NODE_ENV"
          values={form.env_allowlist}
          onChange={(v) => set("env_allowlist", v)}
          help="Host variables agents in this project may see."
        />
      </div>
      <TextAreaField
        className="sm:col-span-3"
        label="Artifact rules (JSON)"
        mono
        rows={3}
        value={form.artifact_rules}
        onChange={(v) => set("artifact_rules", v)}
        error={errors.artifact_rules}
        placeholder='{"collect": ["reports/*.xml"]}'
      />
      <div className="sm:col-span-3">
        <CheckboxField
          label="Run agents in an isolated Git worktree"
          help="Each run works in its own AgentTape-owned worktree, so your checkout is never touched."
          checked={form.git_isolation}
          onChange={(v) => set("git_isolation", v)}
        />
      </div>
    </div>
  );
}
