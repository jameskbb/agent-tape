/**
 * Form primitives shared by the Prompts, Agents, Projects, and onboarding screens.
 * Every control is labelled, shows its help and error text through aria-describedby, and
 * uses the design tokens (never raw colours).
 */
import { Plus, X } from "lucide-react";
import {
  useId,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from "react";

import { Button, IconButton } from "@/components/ui";
import { cn } from "@/lib/cn";

export const inputClass =
  "h-9 w-full rounded-[var(--radius-control)] border border-line-strong bg-surface px-2.5 text-base text-ink placeholder:text-faint disabled:opacity-60 aria-[invalid=true]:border-[var(--st-failed)]";

export const monoInputClass = cn(inputClass, "font-mono text-[0.8125rem]");

export interface FieldProps {
  label: ReactNode;
  help?: ReactNode;
  error?: string | null;
  warning?: string | null;
  required?: boolean;
  className?: string;
  children: (ids: { id: string; describedBy: string | undefined; invalid: boolean }) => ReactNode;
}

/** Label + control + help/error. The render prop receives the ids to wire up. */
export function Field({ label, help, error, warning, required, className, children }: FieldProps) {
  const id = useId();
  const helpId = `${id}-help`;
  const errId = `${id}-err`;
  const describedBy =
    [help ? helpId : null, error || warning ? errId : null].filter(Boolean).join(" ") || undefined;
  return (
    <div className={cn("flex min-w-0 flex-col gap-1", className)}>
      <label htmlFor={id} className="text-sm font-medium text-ink-soft">
        {label}
        {required ? (
          <span className="ml-1 text-[var(--st-failed)]" aria-hidden>
            *
          </span>
        ) : null}
      </label>
      {children({ id, describedBy, invalid: Boolean(error) })}
      {error ? (
        <p id={errId} className="text-sm text-[var(--st-failed)]">
          {error}
        </p>
      ) : warning ? (
        <p id={errId} className="text-sm text-[var(--st-cancelling)]">
          {warning}
        </p>
      ) : null}
      {help ? (
        <p id={helpId} className="text-xs text-muted">
          {help}
        </p>
      ) : null}
    </div>
  );
}

type TextFieldProps = Omit<FieldProps, "children"> &
  Omit<InputHTMLAttributes<HTMLInputElement>, "onChange" | "value"> & {
    value: string;
    onChange: (value: string) => void;
    mono?: boolean;
  };

export function TextField({
  label,
  help,
  error,
  warning,
  required,
  className,
  value,
  onChange,
  mono,
  ...rest
}: TextFieldProps) {
  return (
    <Field
      label={label}
      help={help}
      error={error}
      warning={warning}
      required={required}
      className={className}
    >
      {({ id, describedBy, invalid }) => (
        <input
          id={id}
          aria-describedby={describedBy}
          aria-invalid={invalid}
          value={value}
          required={required}
          onChange={(e) => onChange(e.target.value)}
          className={mono ? monoInputClass : inputClass}
          {...rest}
        />
      )}
    </Field>
  );
}

type TextAreaFieldProps = Omit<FieldProps, "children"> &
  Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, "onChange" | "value"> & {
    value: string;
    onChange: (value: string) => void;
    mono?: boolean;
  };

export function TextAreaField({
  label,
  help,
  error,
  warning,
  required,
  className,
  value,
  onChange,
  mono,
  rows = 4,
  ...rest
}: TextAreaFieldProps) {
  return (
    <Field
      label={label}
      help={help}
      error={error}
      warning={warning}
      required={required}
      className={className}
    >
      {({ id, describedBy, invalid }) => (
        <textarea
          id={id}
          aria-describedby={describedBy}
          aria-invalid={invalid}
          value={value}
          rows={rows}
          required={required}
          onChange={(e) => onChange(e.target.value)}
          className={cn(
            "w-full rounded-[var(--radius-control)] border border-line-strong bg-surface px-2.5 py-2 text-base text-ink placeholder:text-faint aria-[invalid=true]:border-[var(--st-failed)]",
            mono && "font-mono text-[0.8125rem] leading-relaxed",
          )}
          {...rest}
        />
      )}
    </Field>
  );
}

type SelectFieldProps = Omit<FieldProps, "children"> &
  Omit<SelectHTMLAttributes<HTMLSelectElement>, "onChange" | "value"> & {
    value: string;
    onChange: (value: string) => void;
    options: { value: string; label: string }[];
  };

export function SelectField({
  label,
  help,
  error,
  warning,
  required,
  className,
  value,
  onChange,
  options,
  ...rest
}: SelectFieldProps) {
  return (
    <Field
      label={label}
      help={help}
      error={error}
      warning={warning}
      required={required}
      className={className}
    >
      {({ id, describedBy, invalid }) => (
        <select
          id={id}
          aria-describedby={describedBy}
          aria-invalid={invalid}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className={inputClass}
          {...rest}
        >
          {options.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      )}
    </Field>
  );
}

export function CheckboxField({
  label,
  help,
  checked,
  onChange,
  disabled,
}: {
  label: ReactNode;
  help?: ReactNode;
  checked: boolean;
  onChange: (value: boolean) => void;
  disabled?: boolean;
}) {
  const id = useId();
  return (
    <div className="flex items-start gap-2.5">
      <input
        id={id}
        type="checkbox"
        checked={checked}
        disabled={disabled}
        aria-describedby={help ? `${id}-help` : undefined}
        onChange={(e) => onChange(e.target.checked)}
        className="mt-1 size-4 accent-[var(--accent)]"
      />
      <div>
        <label htmlFor={id} className="text-sm font-medium text-ink-soft">
          {label}
        </label>
        {help ? (
          <p id={`${id}-help`} className="text-xs text-muted">
            {help}
          </p>
        ) : null}
      </div>
    </div>
  );
}

/**
 * Edits a list of strings, one per row. Used for argument arrays (never a shell string),
 * env-var names, protected paths, tool rules, and tags.
 */
export function ListEditor({
  label,
  help,
  values,
  onChange,
  placeholder,
  addLabel = "Add",
  mono = true,
  error,
  itemLabel = "Item",
}: {
  label: ReactNode;
  help?: ReactNode;
  values: string[];
  onChange: (values: string[]) => void;
  placeholder?: string;
  addLabel?: string;
  mono?: boolean;
  error?: string | null;
  itemLabel?: string;
}) {
  const id = useId();
  return (
    <fieldset className="flex min-w-0 flex-col gap-1.5" aria-describedby={`${id}-help`}>
      <legend className="text-sm font-medium text-ink-soft">{label}</legend>
      {values.length === 0 ? <p className="text-sm text-faint">None</p> : null}
      <ol className="flex flex-col gap-1.5">
        {values.map((value, index) => (
          <li key={index} className="flex items-center gap-1.5">
            <span className="w-6 shrink-0 text-right text-xs text-faint tabular-nums">{index}</span>
            <input
              aria-label={`${itemLabel} ${index + 1}`}
              value={value}
              placeholder={placeholder}
              onChange={(e) => {
                const next = [...values];
                next[index] = e.target.value;
                onChange(next);
              }}
              className={mono ? monoInputClass : inputClass}
            />
            <IconButton
              size="sm"
              label={`Remove ${itemLabel.toLowerCase()} ${index + 1}`}
              icon={<X className="size-3.5" />}
              onClick={() => onChange(values.filter((_, i) => i !== index))}
            />
          </li>
        ))}
      </ol>
      <div>
        <Button
          size="sm"
          variant="ghost"
          icon={<Plus className="size-3.5" aria-hidden />}
          onClick={() => onChange([...values, ""])}
        >
          {addLabel}
        </Button>
      </div>
      {error ? <p className="text-sm text-[var(--st-failed)]">{error}</p> : null}
      {help ? (
        <p id={`${id}-help`} className="text-xs text-muted">
          {help}
        </p>
      ) : null}
    </fieldset>
  );
}

/** Edits a list of argument arrays (commands), each shown as one row of arguments. */
export function CommandListEditor({
  label,
  help,
  commands,
  onChange,
  error,
}: {
  label: ReactNode;
  help?: ReactNode;
  commands: string[][];
  onChange: (commands: string[][]) => void;
  error?: string | null;
}) {
  return (
    <fieldset className="flex min-w-0 flex-col gap-2">
      <legend className="text-sm font-medium text-ink-soft">{label}</legend>
      {commands.length === 0 ? <p className="text-sm text-faint">None</p> : null}
      {commands.map((argv, index) => (
        <div
          key={index}
          className="flex flex-col gap-1.5 rounded-[var(--radius-control)] border border-line bg-sunken p-2.5"
        >
          <div className="flex items-center justify-between">
            <span className="text-xs text-muted">Command {index + 1}</span>
            <IconButton
              size="sm"
              label={`Remove command ${index + 1}`}
              icon={<X className="size-3.5" />}
              onClick={() => onChange(commands.filter((_, i) => i !== index))}
            />
          </div>
          <div className="flex flex-wrap gap-1.5">
            {argv.map((arg, argIndex) => (
              <span key={argIndex} className="flex items-center">
                <input
                  aria-label={`Command ${index + 1} argument ${argIndex + 1}`}
                  value={arg}
                  size={Math.max(6, arg.length + 1)}
                  onChange={(e) => {
                    const next = commands.map((c) => [...c]);
                    const row = next[index];
                    if (row) row[argIndex] = e.target.value;
                    onChange(next);
                  }}
                  className="h-8 rounded-l-[var(--radius-control)] border border-line-strong bg-surface px-2 font-mono text-[0.8125rem] text-ink"
                />
                <button
                  type="button"
                  aria-label={`Remove argument ${argIndex + 1} of command ${index + 1}`}
                  onClick={() => {
                    const next = commands.map((c) => [...c]);
                    next[index] = (next[index] ?? []).filter((_, i) => i !== argIndex);
                    onChange(next);
                  }}
                  className="h-8 rounded-r-[var(--radius-control)] border border-l-0 border-line-strong bg-surface px-1.5 text-muted hover:text-ink"
                >
                  <X className="size-3" aria-hidden />
                </button>
              </span>
            ))}
            <Button
              size="sm"
              variant="ghost"
              icon={<Plus className="size-3.5" aria-hidden />}
              onClick={() => {
                const next = commands.map((c) => [...c]);
                next[index] = [...(next[index] ?? []), ""];
                onChange(next);
              }}
            >
              Argument
            </Button>
          </div>
        </div>
      ))}
      <div>
        <Button
          size="sm"
          variant="ghost"
          icon={<Plus className="size-3.5" aria-hidden />}
          onClick={() => onChange([...commands, [""]])}
        >
          Add command
        </Button>
      </div>
      {error ? <p className="text-sm text-[var(--st-failed)]">{error}</p> : null}
      {help ? <p className="text-xs text-muted">{help}</p> : null}
    </fieldset>
  );
}

/** Edits a string-to-string map as rows of key and value. */
export function KeyValueEditor({
  label,
  help,
  entries,
  onChange,
  keyPlaceholder,
  valuePlaceholder,
  error,
}: {
  label: ReactNode;
  help?: ReactNode;
  entries: [string, string][];
  onChange: (entries: [string, string][]) => void;
  keyPlaceholder?: string;
  valuePlaceholder?: string;
  error?: string | null;
}) {
  return (
    <fieldset className="flex min-w-0 flex-col gap-1.5">
      <legend className="text-sm font-medium text-ink-soft">{label}</legend>
      {entries.length === 0 ? <p className="text-sm text-faint">None</p> : null}
      {entries.map(([k, v], index) => (
        <div key={index} className="flex items-center gap-1.5">
          <input
            aria-label={`Name ${index + 1}`}
            value={k}
            placeholder={keyPlaceholder}
            onChange={(e) => {
              const next = entries.map((pair) => [...pair] as [string, string]);
              next[index] = [e.target.value, v];
              onChange(next);
            }}
            className={monoInputClass}
          />
          <span aria-hidden className="text-faint">
            =
          </span>
          <input
            aria-label={`Value ${index + 1}`}
            value={v}
            placeholder={valuePlaceholder}
            onChange={(e) => {
              const next = entries.map((pair) => [...pair] as [string, string]);
              next[index] = [k, e.target.value];
              onChange(next);
            }}
            className={monoInputClass}
          />
          <IconButton
            size="sm"
            label={`Remove row ${index + 1}`}
            icon={<X className="size-3.5" />}
            onClick={() => onChange(entries.filter((_, i) => i !== index))}
          />
        </div>
      ))}
      <div>
        <Button
          size="sm"
          variant="ghost"
          icon={<Plus className="size-3.5" aria-hidden />}
          onClick={() => onChange([...entries, ["", ""]])}
        >
          Add
        </Button>
      </div>
      {error ? <p className="text-sm text-[var(--st-failed)]">{error}</p> : null}
      {help ? <p className="text-xs text-muted">{help}</p> : null}
    </fieldset>
  );
}

/** Comma-separated tag entry shown as removable chips. */
export function TagInput({
  label,
  tags,
  onChange,
  help,
}: {
  label: ReactNode;
  tags: string[];
  onChange: (tags: string[]) => void;
  help?: ReactNode;
}) {
  const id = useId();
  const add = (raw: string) => {
    const parts = raw
      .split(",")
      .map((t) => t.trim())
      .filter(Boolean);
    const next = [...tags];
    for (const p of parts) if (!next.includes(p)) next.push(p);
    onChange(next);
  };
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-sm font-medium text-ink-soft">
        {label}
      </label>
      <div className="flex min-h-9 flex-wrap items-center gap-1.5 rounded-[var(--radius-control)] border border-line-strong bg-surface px-2 py-1">
        {tags.map((t) => (
          <span
            key={t}
            className="inline-flex items-center gap-1 rounded bg-sunken px-1.5 py-0.5 text-sm text-ink-soft"
          >
            {t}
            <button
              type="button"
              aria-label={`Remove tag ${t}`}
              onClick={() => onChange(tags.filter((x) => x !== t))}
              className="text-muted hover:text-ink"
            >
              <X className="size-3" aria-hidden />
            </button>
          </span>
        ))}
        <input
          id={id}
          placeholder={tags.length ? "" : "Type and press Enter"}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === ",") {
              e.preventDefault();
              add(e.currentTarget.value);
              e.currentTarget.value = "";
            } else if (e.key === "Backspace" && !e.currentTarget.value && tags.length) {
              onChange(tags.slice(0, -1));
            }
          }}
          onBlur={(e) => {
            if (e.currentTarget.value.trim()) {
              add(e.currentTarget.value);
              e.currentTarget.value = "";
            }
          }}
          className="h-7 min-w-[8rem] flex-1 bg-transparent text-base text-ink outline-none placeholder:text-faint"
        />
      </div>
      {help ? <p className="text-xs text-muted">{help}</p> : null}
    </div>
  );
}
