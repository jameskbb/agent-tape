import { useState } from "react";

import {
  CheckboxField,
  CommandListEditor,
  KeyValueEditor,
  ListEditor,
  SelectField,
  TextAreaField,
  TextField,
} from "@/components/forms";
import { Badge } from "@/components/ui";
import { cn } from "@/lib/cn";

import type { ProfileConfig, ValidationIssue } from "./api";
import {
  configToForm,
  fieldsFor,
  formToConfig,
  type FieldSpec,
  type FieldValue,
  type FormState,
} from "./configForm";

const GROUP_ORDER: FieldSpec["group"][] = [
  "Scenario",
  "Command",
  "Model and permissions",
  "Environment",
  "Limits",
  "Checks",
];

function issuesFor(path: string, issues: ValidationIssue[]) {
  return issues.filter(
    (i) => i.field === path || i.field.startsWith(`${path}[`) || i.field.startsWith(`${path}.`),
  );
}

function FieldControl({
  spec,
  value,
  onChange,
  issues,
}: {
  spec: FieldSpec;
  value: FieldValue | undefined;
  onChange: (v: FieldValue) => void;
  issues: ValidationIssue[];
}) {
  const unique = (list: string[]) => [...new Set(list)];
  const errors = unique(issues.filter((i) => i.severity === "error").map((i) => i.message));
  const warnings = unique(issues.filter((i) => i.severity !== "error").map((i) => i.message));
  const error = errors.join("; ") || null;
  const warning = warnings.join("; ") || null;
  switch (spec.kind) {
    case "bool":
      return (
        <div className="sm:col-span-2">
          <CheckboxField
            label={spec.label}
            help={spec.help}
            checked={value === true}
            onChange={onChange}
          />
        </div>
      );
    case "select":
      return (
        <SelectField
          label={spec.label}
          help={spec.help}
          error={error}
          warning={warning}
          value={typeof value === "string" ? value : ""}
          onChange={onChange}
          options={spec.options ?? []}
        />
      );
    case "list":
      return (
        <div className="sm:col-span-2">
          <ListEditor
            label={spec.label}
            help={spec.help}
            itemLabel={spec.label}
            values={Array.isArray(value) ? (value as string[]) : []}
            onChange={onChange}
            error={error ?? warning}
          />
        </div>
      );
    case "commands":
      return (
        <div className="sm:col-span-2">
          <CommandListEditor
            label={spec.label}
            help={spec.help}
            commands={Array.isArray(value) ? (value as string[][]) : []}
            onChange={onChange}
            error={error ?? warning}
          />
        </div>
      );
    case "map":
      return (
        <div className="sm:col-span-2">
          <KeyValueEditor
            label={spec.label}
            help={spec.help}
            entries={Array.isArray(value) ? (value as [string, string][]) : []}
            onChange={onChange}
            keyPlaceholder="ENV_NAME"
            valuePlaceholder="SECRET_REFERENCE"
            error={error ?? warning}
          />
        </div>
      );
    default:
      return (
        <TextField
          label={spec.label}
          help={spec.help}
          error={error}
          warning={warning}
          placeholder={spec.placeholder}
          mono={spec.path === "executable" || spec.path === "working_dir"}
          inputMode={spec.kind === "number" ? "decimal" : undefined}
          value={typeof value === "string" ? value : ""}
          onChange={onChange}
          spellCheck={false}
        />
      );
  }
}

/**
 * Structured editor for one adapter's profile config, with an "Advanced JSON" view.
 * ``config`` is the source of truth; the form is derived from it on every change so the
 * two views never drift.
 */
export function ConfigEditor({
  adapterType,
  config,
  onChange,
  issues,
}: {
  adapterType: string;
  config: ProfileConfig;
  onChange: (config: ProfileConfig) => void;
  issues: ValidationIssue[];
}) {
  const fields = fieldsFor(adapterType);
  const [mode, setMode] = useState<"form" | "json">("form");
  const [form, setForm] = useState<FormState>(() => configToForm(config, fields));
  const [jsonText, setJsonText] = useState(() => JSON.stringify(config, null, 2));
  const [jsonError, setJsonError] = useState<string | null>(null);

  const setField = (path: string, value: FieldValue) => {
    const next = { ...form, [path]: value };
    setForm(next);
    onChange(formToConfig(next, fields, config));
  };

  const switchTo = (next: "form" | "json") => {
    if (next === mode) return;
    if (next === "json") {
      setJsonText(JSON.stringify(config, null, 2));
      setJsonError(null);
    } else {
      setForm(configToForm(config, fields));
    }
    setMode(next);
  };

  const workdirSub = form.workdir_mode === "subdirectory";
  const shownPaths = new Set(fields.map((f) => f.path));
  const orphanIssues = issues.filter(
    (i) => ![...shownPaths].some((p) => issuesFor(p, [i]).length > 0) || mode === "json",
  );

  return (
    <div className="flex flex-col gap-4">
      <div
        role="radiogroup"
        aria-label="Editor mode"
        className="inline-flex self-start rounded-[var(--radius-control)] border border-line-strong p-0.5"
      >
        {(["form", "json"] as const).map((m) => (
          <button
            key={m}
            type="button"
            role="radio"
            aria-checked={mode === m}
            onClick={() => switchTo(m)}
            className={cn(
              "rounded-[4px] px-3 py-1 text-sm",
              mode === m ? "bg-accent-soft font-medium text-ink" : "text-muted hover:text-ink",
            )}
          >
            {m === "form" ? "Form" : "Advanced JSON"}
          </button>
        ))}
      </div>

      {mode === "json" ? (
        <TextAreaField
          label="Configuration JSON"
          mono
          rows={16}
          value={jsonText}
          error={jsonError}
          help="Every key is kept, including ones the form does not show (evaluators, retry). Validation runs on the parsed object."
          onChange={(text) => {
            setJsonText(text);
            try {
              const parsed: unknown = JSON.parse(text);
              if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
                setJsonError("The configuration must be a JSON object.");
                return;
              }
              setJsonError(null);
              onChange(parsed as ProfileConfig);
            } catch {
              setJsonError("Not valid JSON yet.");
            }
          }}
        />
      ) : (
        GROUP_ORDER.map((group) => {
          const specs = fields.filter(
            (f) => f.group === group && (f.path !== "working_dir" || workdirSub),
          );
          if (!specs.length) return null;
          return (
            <fieldset
              key={group}
              className="flex flex-col gap-3 border-t border-line pt-4 first:border-t-0 first:pt-0"
            >
              <legend className="mb-1 text-sm font-semibold text-ink">{group}</legend>
              <div className="grid gap-4 sm:grid-cols-2">
                {specs.map((spec) => (
                  <FieldControl
                    key={spec.path}
                    spec={spec}
                    value={form[spec.path]}
                    onChange={(v) => setField(spec.path, v)}
                    issues={issuesFor(spec.path, issues)}
                  />
                ))}
              </div>
            </fieldset>
          );
        })
      )}

      {orphanIssues.length ? (
        <ul className="flex flex-col gap-1 text-sm" aria-label="Other validation results">
          {orphanIssues.map((i, k) => (
            <li key={k} className="flex items-start gap-2">
              <Badge tone={i.severity === "error" ? "danger" : "warning"}>{i.severity}</Badge>
              <span>
                <code>{i.field}</code> {i.message}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
