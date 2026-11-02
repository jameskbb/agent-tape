/**
 * Pure helpers for prompt variables: template tokens, definition clean-up, form values, and
 * input coercion. The server is the authority on validation; these give instant feedback.
 */
import type { VariableDefinition, VariableType } from "./api";

const TOKEN = /\{\{\s*(secret:)?\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}/g;
export const VARIABLE_NAME = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;
export const SECRET_NAME = /^[A-Za-z_][A-Za-z0-9_]{0,127}$/;

/** Variable and secret names referenced by a template body, in first-use order. */
export function bodyTokens(body: string): { variables: string[]; secrets: string[] } {
  const variables: string[] = [];
  const secrets: string[] = [];
  for (const match of body.matchAll(TOKEN)) {
    const name = match[2] ?? "";
    const target = match[1] ? secrets : variables;
    if (name && !target.includes(name)) target.push(name);
  }
  return { variables, secrets };
}

export function newVariable(name = ""): VariableDefinition {
  return { name, type: "string", required: true, default: null, help: null };
}

function blank(value: unknown): boolean {
  return value === null || value === undefined || value === "";
}

/** Coerce a default typed in a form (always a string or boolean) into the variable's type. */
export function coerceDefault(type: VariableType, raw: unknown): unknown {
  if (blank(raw)) return null;
  if (type === "number") {
    const n = typeof raw === "number" ? raw : Number(raw);
    return Number.isFinite(n) ? n : raw;
  }
  if (type === "boolean") {
    if (typeof raw === "boolean") return raw;
    return String(raw).toLowerCase() === "true";
  }
  return typeof raw === "string" ? raw : JSON.stringify(raw);
}

/** Remove empty optional fields and keep only the fields that apply to the type. */
export function cleanVariables(defs: VariableDefinition[]): VariableDefinition[] {
  return defs.map((d) => {
    const out: VariableDefinition = {
      name: d.name.trim(),
      type: d.type,
      required: d.required ?? true,
    };
    if (d.label?.trim()) out.label = d.label.trim();
    if (d.help?.trim()) out.help = d.help.trim();
    const def = coerceDefault(d.type, d.default);
    if (def !== null) out.default = def;
    if (d.type === "enum") out.options = (d.options ?? []).map((o) => o.trim()).filter(Boolean);
    const v = d.validation ?? {};
    const validation: NonNullable<VariableDefinition["validation"]> = {};
    if (d.type === "string" || d.type === "multiline" || d.type === "path") {
      if (v.pattern) validation.pattern = v.pattern;
      if (typeof v.min_length === "number") validation.min_length = v.min_length;
      if (typeof v.max_length === "number") validation.max_length = v.max_length;
    }
    if (d.type === "number") {
      if (typeof v.minimum === "number") validation.minimum = v.minimum;
      if (typeof v.maximum === "number") validation.maximum = v.maximum;
    }
    if (Object.keys(validation).length) out.validation = validation;
    return out;
  });
}

export interface LocalIssue {
  variable: string;
  message: string;
}

/** Instant checks mirroring the server's save-time validation. */
export function definitionIssues(body: string, defs: VariableDefinition[]): LocalIssue[] {
  const issues: LocalIssue[] = [];
  const names = defs.map((d) => d.name.trim());
  names.forEach((name, i) => {
    if (!VARIABLE_NAME.test(name)) {
      issues.push({
        variable: name || `#${i + 1}`,
        message: "Names use letters, digits, and _ and cannot start with a digit",
      });
    } else if (names.indexOf(name) !== i) {
      issues.push({ variable: name, message: "is declared twice" });
    }
  });
  defs.forEach((d) => {
    if (d.type === "enum" && !(d.options ?? []).some((o) => o.trim())) {
      issues.push({ variable: d.name, message: "needs at least one option" });
    }
    if (d.validation?.pattern) {
      try {
        new RegExp(d.validation.pattern);
      } catch {
        issues.push({ variable: d.name, message: "has an invalid pattern" });
      }
    }
    if (d.type === "secret" && typeof d.default === "string" && d.default) {
      if (!SECRET_NAME.test(d.default)) {
        issues.push({
          variable: d.name,
          message: "default must be a secret reference name (for example GITHUB_TOKEN)",
        });
      }
    }
  });
  for (const used of bodyTokens(body).variables) {
    if (!names.includes(used)) {
      issues.push({ variable: used, message: "is used in the template but not declared" });
    }
  }
  return issues;
}

export type FormValue = string | boolean;

/** The initial form value for a variable (its default, or empty). */
export function initialFormValue(def: VariableDefinition): FormValue {
  if (def.type === "boolean") return def.default === true || def.default === "true";
  if (blank(def.default)) return "";
  return typeof def.default === "string" ? def.default : JSON.stringify(def.default);
}

export function initialForm(defs: VariableDefinition[]): Record<string, FormValue> {
  return Object.fromEntries(defs.map((d) => [d.name, initialFormValue(d)]));
}

/**
 * Turn form values into run inputs. Empty values are omitted so the server applies the
 * default (or reports a missing required variable); numbers are sent as numbers when they
 * parse, otherwise as text so the server can explain the problem.
 */
export function buildInputs(
  defs: VariableDefinition[],
  form: Record<string, FormValue>,
): Record<string, unknown> {
  const inputs: Record<string, unknown> = {};
  for (const d of defs) {
    const raw = form[d.name];
    if (raw === undefined) continue;
    if (d.type === "boolean") {
      inputs[d.name] = raw === true || raw === "true";
      continue;
    }
    if (typeof raw !== "string" || (raw === "" && d.type !== "multiline")) continue;
    if (raw === "" && d.type === "multiline" && blank(d.default)) continue;
    if (d.type === "number") {
      const n = Number(raw);
      inputs[d.name] = raw.trim() !== "" && Number.isFinite(n) ? n : raw;
      continue;
    }
    inputs[d.name] = raw;
  }
  return inputs;
}
