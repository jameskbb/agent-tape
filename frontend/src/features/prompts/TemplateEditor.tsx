import { ArrowDown, ArrowUp, ChevronDown, ChevronRight, Plus, Trash2, Wand2 } from "lucide-react";
import { useState } from "react";

import {
  CheckboxField,
  ListEditor,
  SelectField,
  TextAreaField,
  TextField,
} from "@/components/forms";
import { Badge, Button, IconButton } from "@/components/ui";
import { cn } from "@/lib/cn";

import { VARIABLE_TYPES, type VariableDefinition, type VariableType } from "./api";
import { bodyTokens, definitionIssues, newVariable } from "./variables";

function numOrNull(raw: string): number | null {
  if (raw.trim() === "") return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
}

function numText(value: number | null | undefined): string {
  return typeof value === "number" ? String(value) : "";
}

function VariableCard({
  def,
  index,
  count,
  used,
  onChange,
  onRemove,
  onMove,
}: {
  def: VariableDefinition;
  index: number;
  count: number;
  used: boolean;
  onChange: (def: VariableDefinition) => void;
  onRemove: () => void;
  onMove: (delta: number) => void;
}) {
  const v = def.validation ?? {};
  const setValidation = (patch: Partial<NonNullable<VariableDefinition["validation"]>>) =>
    onChange({ ...def, validation: { ...v, ...patch } });
  const defaultText =
    def.default === null || def.default === undefined
      ? ""
      : typeof def.default === "string"
        ? def.default
        : JSON.stringify(def.default);
  const typeInfo = VARIABLE_TYPES.find((t) => t.value === def.type);
  const hint = typeInfo?.hint;
  const [open, setOpen] = useState(!def.name);
  const summary = [
    typeInfo?.label ?? def.type,
    def.required === false ? "optional" : "required",
    defaultText ? `default ${defaultText}` : null,
  ]
    .filter(Boolean)
    .join(", ");
  return (
    <li
      className="rounded-[var(--radius-panel)] border border-line bg-surface"
      aria-label={`Variable ${def.name || index + 1}`}
    >
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line bg-sunken px-3 py-2">
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
          className="flex min-w-0 flex-1 items-center gap-2 text-left"
        >
          {open ? (
            <ChevronDown aria-hidden className="size-4 shrink-0 text-muted" />
          ) : (
            <ChevronRight aria-hidden className="size-4 shrink-0 text-muted" />
          )}
          <code className="truncate text-sm text-ink">{`{{ ${def.name || "name"} }}`}</code>
          <span className="truncate text-xs text-muted">{summary}</span>
          {used ? null : (
            <Badge tone="warning" title="Declared but not used in the template body">
              Not used
            </Badge>
          )}
        </button>
        <div className="flex items-center gap-0.5">
          <IconButton
            size="sm"
            label="Move up"
            icon={<ArrowUp className="size-3.5" />}
            disabled={index === 0}
            onClick={() => onMove(-1)}
          />
          <IconButton
            size="sm"
            label="Move down"
            icon={<ArrowDown className="size-3.5" />}
            disabled={index === count - 1}
            onClick={() => onMove(1)}
          />
          <IconButton
            size="sm"
            label={`Remove variable ${def.name || index + 1}`}
            icon={<Trash2 className="size-3.5" />}
            onClick={onRemove}
          />
        </div>
      </div>
      {open ? (
        <div className="grid gap-3 p-3 sm:grid-cols-2">
          <TextField
            label="Name"
            mono
            required
            value={def.name}
            spellCheck={false}
            onChange={(name) => onChange({ ...def, name })}
          />
          <SelectField
            label="Type"
            help={hint}
            value={def.type}
            onChange={(type) => {
              const t = type as VariableType;
              onChange({
                ...def,
                type: t,
                default: t === "boolean" ? false : null,
                options: t === "enum" ? (def.options ?? []) : null,
              });
            }}
            options={VARIABLE_TYPES.map((t) => ({ value: t.value, label: t.label }))}
          />
          <TextField
            label="Label"
            value={def.label ?? ""}
            placeholder="Shown instead of the name"
            onChange={(label) => onChange({ ...def, label })}
          />
          {def.type === "boolean" ? (
            <SelectField
              label="Default"
              value={def.default === true ? "true" : "false"}
              onChange={(val) => onChange({ ...def, default: val === "true" })}
              options={[
                { value: "false", label: "false" },
                { value: "true", label: "true" },
              ]}
            />
          ) : def.type === "enum" ? (
            <SelectField
              label="Default"
              value={defaultText}
              onChange={(val) => onChange({ ...def, default: val || null })}
              options={[
                { value: "", label: "(no default)" },
                ...(def.options ?? []).filter(Boolean).map((o) => ({ value: o, label: o })),
              ]}
            />
          ) : (
            <TextField
              label="Default"
              mono={def.type === "secret" || def.type === "path"}
              value={defaultText}
              inputMode={def.type === "number" ? "decimal" : undefined}
              placeholder={def.type === "secret" ? "SECRET_NAME (never a value)" : "(no default)"}
              onChange={(val) => onChange({ ...def, default: val })}
            />
          )}
          <TextField
            className="sm:col-span-2"
            label="Help text"
            value={def.help ?? ""}
            placeholder="Explain what to enter"
            onChange={(help) => onChange({ ...def, help })}
          />
          <div className="sm:col-span-2">
            <CheckboxField
              label="Required"
              help="Runs cannot start without a value (a default counts as a value)."
              checked={def.required !== false}
              onChange={(required) => onChange({ ...def, required })}
            />
          </div>
          {def.type === "enum" ? (
            <div className="sm:col-span-2">
              <ListEditor
                label="Options"
                itemLabel="Option"
                addLabel="Add option"
                mono={false}
                values={def.options ?? []}
                onChange={(options) => onChange({ ...def, options })}
              />
            </div>
          ) : null}
          {def.type === "string" || def.type === "multiline" || def.type === "path" ? (
            <>
              <TextField
                className="sm:col-span-2"
                label="Pattern"
                mono
                value={v.pattern ?? ""}
                help="Optional regular expression the whole value must match."
                onChange={(pattern) => setValidation({ pattern: pattern || null })}
              />
              <TextField
                label="Minimum length"
                inputMode="numeric"
                value={numText(v.min_length)}
                onChange={(raw) => setValidation({ min_length: numOrNull(raw) })}
              />
              <TextField
                label="Maximum length"
                inputMode="numeric"
                value={numText(v.max_length)}
                onChange={(raw) => setValidation({ max_length: numOrNull(raw) })}
              />
            </>
          ) : null}
          {def.type === "number" ? (
            <>
              <TextField
                label="Minimum"
                inputMode="decimal"
                value={numText(v.minimum)}
                onChange={(raw) => setValidation({ minimum: numOrNull(raw) })}
              />
              <TextField
                label="Maximum"
                inputMode="decimal"
                value={numText(v.maximum)}
                onChange={(raw) => setValidation({ maximum: numOrNull(raw) })}
              />
            </>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}

/** Template body plus its typed variables, with instant checks. */
export function TemplateEditor({
  body,
  variables,
  onBody,
  onVariables,
  readOnly = false,
}: {
  body: string;
  variables: VariableDefinition[];
  onBody: (body: string) => void;
  onVariables: (defs: VariableDefinition[]) => void;
  readOnly?: boolean;
}) {
  const tokens = bodyTokens(body);
  const declared = new Set(variables.map((v) => v.name));
  const missing = tokens.variables.filter((n) => !declared.has(n));
  const issues = definitionIssues(body, variables);
  const move = (index: number, delta: number) => {
    const next = [...variables];
    const [item] = next.splice(index, 1);
    if (item) next.splice(index + delta, 0, item);
    onVariables(next);
  };
  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-col gap-2">
        <TextAreaField
          label="Template"
          mono
          rows={10}
          required
          readOnly={readOnly}
          value={body}
          onChange={onBody}
          spellCheck={false}
          help={
            <>
              Insert a variable with <code>{"{{ name }}"}</code> and a secret reference with{" "}
              <code>{"{{ secret:NAME }}"}</code>. Secret values are resolved only when a run
              executes and are stored as <code>[secret:NAME]</code>.
            </>
          }
        />
        {tokens.variables.length || tokens.secrets.length ? (
          <div className="flex flex-wrap items-center gap-1.5" aria-label="Referenced in template">
            <span className="text-xs text-muted">Referenced:</span>
            {tokens.variables.map((n) => (
              <Badge key={`v-${n}`} tone={declared.has(n) ? "accent" : "danger"}>
                {n}
                {declared.has(n) ? "" : " (undeclared)"}
              </Badge>
            ))}
            {tokens.secrets.map((n) => (
              <Badge key={`s-${n}`} tone="violet" title="Secret reference">
                secret:{n}
              </Badge>
            ))}
          </div>
        ) : null}
      </div>

      <section aria-labelledby="vars-heading" className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 id="vars-heading" className="text-base font-semibold">
            Variables <span className="font-normal text-muted">({variables.length})</span>
          </h3>
          {readOnly ? null : (
            <div className="flex flex-wrap gap-2">
              {missing.length ? (
                <Button
                  size="sm"
                  icon={<Wand2 className="size-3.5" aria-hidden />}
                  onClick={() => onVariables([...variables, ...missing.map((n) => newVariable(n))])}
                >
                  Declare {missing.length} from template
                </Button>
              ) : null}
              <Button
                size="sm"
                icon={<Plus className="size-3.5" aria-hidden />}
                onClick={() => onVariables([...variables, newVariable()])}
              >
                Add variable
              </Button>
            </div>
          )}
        </div>
        {variables.length === 0 ? (
          <p className="text-sm text-muted">
            No variables yet. Write <code>{"{{ name }}"}</code> in the template, then declare it
            here to give it a type, default, and validation.
          </p>
        ) : (
          <ol className={cn("flex flex-col gap-3", readOnly && "pointer-events-none opacity-70")}>
            {variables.map((def, index) => (
              <VariableCard
                key={index}
                def={def}
                index={index}
                count={variables.length}
                used={tokens.variables.includes(def.name)}
                onChange={(d) => onVariables(variables.map((x, i) => (i === index ? d : x)))}
                onRemove={() => onVariables(variables.filter((_, i) => i !== index))}
                onMove={(delta) => move(index, delta)}
              />
            ))}
          </ol>
        )}
        {issues.length ? (
          <ul
            role="alert"
            className="flex flex-col gap-1 rounded-[var(--radius-control)] border border-[color-mix(in_srgb,var(--st-failed)_35%,transparent)] bg-[var(--st-failed-bg)] px-3 py-2 text-sm"
          >
            {issues.map((i, k) => (
              <li key={k}>
                <code>{i.variable}</code> {i.message}
              </li>
            ))}
          </ul>
        ) : null}
      </section>
    </div>
  );
}
