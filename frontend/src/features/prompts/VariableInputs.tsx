import { KeyRound } from "lucide-react";

import { CheckboxField, SelectField, TextAreaField, TextField } from "@/components/forms";

import type { VariableDefinition } from "./api";
import type { FormValue } from "./variables";

/** A form for filling a prompt version's typed variables (preview, test, onboarding). */
export function VariableInputs({
  definitions,
  values,
  onChange,
  issues = [],
}: {
  definitions: VariableDefinition[];
  values: Record<string, FormValue>;
  onChange: (values: Record<string, FormValue>) => void;
  issues?: { variable: string; message: string }[];
}) {
  if (definitions.length === 0) {
    return <p className="text-sm text-muted">This prompt has no variables.</p>;
  }
  const set = (name: string, value: FormValue) => onChange({ ...values, [name]: value });
  return (
    <div className="grid gap-4 sm:grid-cols-2">
      {definitions.map((d) => {
        const label = d.label || d.name;
        const error =
          issues
            .filter((i) => i.variable === d.name)
            .map((i) => i.message)
            .join("; ") || null;
        const raw = values[d.name];
        const text = typeof raw === "string" ? raw : "";
        const required = d.required !== false && (d.default === null || d.default === undefined);
        const help = d.help ?? undefined;
        if (d.type === "boolean") {
          return (
            <div key={d.name} className="sm:col-span-2">
              <CheckboxField
                label={label}
                help={help}
                checked={raw === true}
                onChange={(v) => set(d.name, v)}
              />
            </div>
          );
        }
        if (d.type === "enum") {
          return (
            <SelectField
              key={d.name}
              label={label}
              help={help}
              error={error}
              required={required}
              value={text}
              onChange={(v) => set(d.name, v)}
              options={[
                ...(required ? [] : [{ value: "", label: "(none)" }]),
                ...(text === "" && required ? [{ value: "", label: "Choose..." }] : []),
                ...(d.options ?? []).map((o) => ({ value: o, label: o })),
              ]}
            />
          );
        }
        if (d.type === "multiline") {
          return (
            <TextAreaField
              key={d.name}
              className="sm:col-span-2"
              label={label}
              help={help}
              error={error}
              required={required}
              value={text}
              rows={3}
              onChange={(v) => set(d.name, v)}
            />
          );
        }
        if (d.type === "secret") {
          return (
            <TextField
              key={d.name}
              mono
              label={
                <span className="inline-flex items-center gap-1.5">
                  <KeyRound aria-hidden className="size-3.5 text-muted" />
                  {label}
                </span>
              }
              help={
                help ??
                "Enter the secret's reference name, never its value. The value is read from the environment only when the run executes."
              }
              error={error}
              required={required}
              value={text}
              autoComplete="off"
              spellCheck={false}
              placeholder="SECRET_NAME"
              onChange={(v) => set(d.name, v)}
            />
          );
        }
        return (
          <TextField
            key={d.name}
            label={label}
            help={help}
            error={error}
            required={required}
            value={text}
            mono={d.type === "path"}
            inputMode={d.type === "number" ? "decimal" : undefined}
            placeholder={d.type === "path" ? "relative/path" : undefined}
            onChange={(v) => set(d.name, v)}
          />
        );
      })}
    </div>
  );
}
