/**
 * Structured profile form <-> adapter config JSON.
 *
 * Each adapter shows the fields that apply to it. Form values live in a flat map keyed by
 * config path ("budget.max_cost_usd"); keys the form does not manage (evaluators, retry,
 * anything added in JSON) are preserved untouched.
 */
import type { ProfileConfig } from "./api";

export type FieldKind = "text" | "number" | "bool" | "select" | "list" | "commands" | "map";

export interface FieldSpec {
  path: string;
  kind: FieldKind;
  label: string;
  help?: string;
  options?: { value: string; label: string }[];
  placeholder?: string;
  group: "Command" | "Model and permissions" | "Environment" | "Limits" | "Checks" | "Scenario";
  integer?: boolean;
}

export type FieldValue = string | boolean | string[] | string[][] | [string, string][];
export type FormState = Record<string, FieldValue>;

const opt = (values: string[]) => values.map((v) => ({ value: v, label: v }));

const EXECUTABLE: FieldSpec = {
  path: "executable",
  kind: "text",
  label: "Executable",
  group: "Command",
  help: "Absolute path or a name on PATH. It must be on the executable allowlist.",
};
const ARGS: FieldSpec = {
  path: "args",
  kind: "list",
  label: "Arguments",
  group: "Command",
  help: "One argument per row, passed exactly as written. No shell is involved, so quotes, pipes, and $VARS are literal.",
};
const WORKDIR_MODE: FieldSpec = {
  path: "workdir_mode",
  kind: "select",
  label: "Working directory",
  group: "Command",
  options: [
    { value: "", label: "Project directory (default)" },
    { value: "project", label: "Project directory" },
    { value: "subdirectory", label: "A subdirectory of the project" },
  ],
};
const WORKING_DIR: FieldSpec = {
  path: "working_dir",
  kind: "text",
  label: "Subdirectory",
  group: "Command",
  placeholder: "packages/api",
  help: "Relative to the project; used when the working directory is a subdirectory.",
};
const MODEL: FieldSpec = {
  path: "model",
  kind: "text",
  label: "Model",
  group: "Model and permissions",
  placeholder: "Leave empty for the CLI default",
};
const ENV_ALLOWLIST: FieldSpec = {
  path: "env_allowlist",
  kind: "list",
  label: "Environment allowlist",
  group: "Environment",
  help: "Names of host environment variables the agent may see. Everything else is withheld (PATH, HOME, and locale are always passed).",
};
const SECRET_ENV: FieldSpec = {
  path: "secret_env",
  kind: "map",
  label: "Secret environment",
  group: "Environment",
  help: "ENV_NAME = SECRET_REFERENCE. Values are read at execution time, redacted from events, and never stored.",
};
const LIMITS: FieldSpec[] = [
  {
    path: "timeout_seconds",
    kind: "number",
    label: "Timeout (seconds)",
    group: "Limits",
    help: "Wall-clock limit per attempt.",
  },
  {
    path: "idle_timeout_seconds",
    kind: "number",
    label: "Idle timeout (seconds)",
    group: "Limits",
    help: "Stop when the agent produces no output for this long.",
  },
  {
    path: "max_attempts",
    kind: "number",
    integer: true,
    label: "Max attempts",
    group: "Limits",
    help: "1 to 20. Retryable failures are retried with backoff.",
  },
];
const BUDGET: FieldSpec = {
  path: "budget.max_cost_usd",
  kind: "number",
  label: "Budget (USD)",
  group: "Limits",
  help: "Only enforced when the adapter reports cost.",
};
const TOOLS: FieldSpec[] = [
  {
    path: "allowed_tools",
    kind: "list",
    label: "Allowed tools",
    group: "Model and permissions",
    help: "Tool rules, for example Read or Bash(git diff *).",
  },
  {
    path: "denied_tools",
    kind: "list",
    label: "Denied tools",
    group: "Model and permissions",
  },
];
const CHECKS: FieldSpec[] = [
  {
    path: "preflight_commands",
    kind: "commands",
    label: "Preflight commands",
    group: "Checks",
    help: "Run before the agent; a non-zero exit fails the attempt.",
  },
  {
    path: "validation_commands",
    kind: "commands",
    label: "Validation commands",
    group: "Checks",
    help: "Run after the agent succeeds; a non-zero exit fails the attempt.",
  },
];
const EXTRA_ARGS: FieldSpec = {
  path: "extra_args",
  kind: "list",
  label: "Extra CLI flags",
  group: "Command",
  help: "Appended to the generated command. Flags that bypass safety checks are refused.",
};

export const ADAPTER_FIELDS: Record<string, FieldSpec[]> = {
  demo: [
    {
      path: "scenario",
      kind: "select",
      label: "Scenario",
      group: "Scenario",
      options: [
        { value: "success", label: "Succeeds" },
        { value: "fail_then_retry", label: "Fails once, then succeeds on retry" },
        { value: "needs_approval", label: "Pauses for approval" },
        { value: "timeout", label: "Runs until the timeout stops it" },
        { value: "fail", label: "Fails its checks" },
      ],
    },
    {
      path: "step_delay_ms",
      kind: "number",
      integer: true,
      label: "Step delay (ms)",
      group: "Scenario",
      help: "Pacing between simulated steps (0 to 10000).",
    },
    ...LIMITS,
  ],
  "generic-command": [
    EXECUTABLE,
    ARGS,
    {
      path: "prompt_mode",
      kind: "select",
      label: "Pass the prompt via",
      group: "Command",
      options: [
        { value: "stdin", label: "Standard input" },
        { value: "env", label: "AGENTTAPE_PROMPT environment variable" },
        { value: "arg", label: "Final argument" },
        { value: "none", label: "Do not pass the prompt" },
      ],
    },
    {
      path: "parser",
      kind: "select",
      label: "Output",
      group: "Command",
      options: [
        { value: "text", label: "Plain text (stdout and stderr)" },
        { value: "jsonl", label: "AgentTape JSONL events" },
      ],
    },
    {
      path: "success_exit_codes",
      kind: "list",
      label: "Success exit codes",
      group: "Command",
      help: "Defaults to 0.",
    },
    WORKDIR_MODE,
    WORKING_DIR,
    ENV_ALLOWLIST,
    SECRET_ENV,
    ...LIMITS,
    ...CHECKS,
  ],
  "claude-code": [
    {
      ...EXECUTABLE,
      help: "Leave empty to use claude from PATH. Other paths must be allowlisted.",
    },
    MODEL,
    {
      path: "permission_mode",
      kind: "select",
      label: "Permission mode",
      group: "Model and permissions",
      options: [
        { value: "", label: "CLI default" },
        ...opt(["default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions"]),
      ],
    },
    {
      path: "max_turns",
      kind: "number",
      integer: true,
      label: "Max turns",
      group: "Limits",
    },
    ...TOOLS,
    {
      path: "bare",
      kind: "bool",
      label: "Bare mode",
      group: "Command",
      help: "Skip hooks, plugins, and CLAUDE.md discovery (--bare).",
    },
    EXTRA_ARGS,
    WORKDIR_MODE,
    WORKING_DIR,
    ENV_ALLOWLIST,
    SECRET_ENV,
    ...LIMITS,
    BUDGET,
    ...CHECKS,
  ],
  "codex-cli": [
    { ...EXECUTABLE, help: "Leave empty to use codex from PATH. Other paths must be allowlisted." },
    MODEL,
    {
      path: "sandbox",
      kind: "select",
      label: "Sandbox",
      group: "Model and permissions",
      options: [
        { value: "", label: "CLI default" },
        ...opt(["read-only", "workspace-write", "danger-full-access"]),
      ],
    },
    {
      path: "skip_git_repo_check",
      kind: "bool",
      label: "Allow running outside a Git repository",
      group: "Command",
    },
    { path: "ephemeral", kind: "bool", label: "Do not keep session files", group: "Command" },
    EXTRA_ARGS,
    WORKDIR_MODE,
    WORKING_DIR,
    ENV_ALLOWLIST,
    SECRET_ENV,
    ...LIMITS,
    ...CHECKS,
  ],
};

// M2: the Herdr runtime adapter (agents live in Herdr; see docs/HERDR.md).
ADAPTER_FIELDS.herdr = [
  {
    path: "agent_kind",
    kind: "select",
    label: "Agent",
    group: "Command",
    options: [
      { value: "claude", label: "Claude Code (started by Herdr)" },
      { value: "codex", label: "Codex (started by Herdr)" },
      { value: "demo", label: "Demo agent (a command in a Herdr pane)" },
    ],
  },
  {
    path: "workspace_policy",
    kind: "select",
    label: "Workspace",
    group: "Command",
    options: [
      { value: "new", label: "New AgentTape-owned workspace per attempt" },
      { value: "reuse", label: "Reuse an existing workspace or agent" },
    ],
  },
  {
    path: "reuse_workspace_id",
    kind: "text",
    label: "Reuse workspace",
    group: "Command",
    placeholder: "w3",
    help: "With Reuse: AgentTape opens its own tab in this Herdr workspace.",
  },
  {
    path: "reuse_agent",
    kind: "text",
    label: "Reuse agent",
    group: "Command",
    placeholder: "reviewer",
    help: "With Reuse: prompt this idle Claude Code or Codex agent instead of starting one.",
  },
  {
    path: "cleanup",
    kind: "select",
    label: "Cleanup",
    group: "Command",
    options: [
      { value: "keep", label: "Keep the workspace" },
      { value: "close_owned", label: "Close it when done (only if AgentTape created it)" },
    ],
  },
  {
    path: "fallback",
    kind: "select",
    label: "If Herdr is unavailable",
    group: "Command",
    options: [
      { value: "none", label: "Fail the run" },
      { value: "local", label: "Run locally (only before the attempt starts)" },
    ],
  },
  ...LIMITS,
];

export function fieldsFor(adapter: string): FieldSpec[] {
  return ADAPTER_FIELDS[adapter] ?? LIMITS;
}

function getPath(obj: ProfileConfig, path: string): unknown {
  let cur: unknown = obj;
  for (const part of path.split(".")) {
    if (cur === null || typeof cur !== "object" || Array.isArray(cur)) return undefined;
    cur = (cur as Record<string, unknown>)[part];
  }
  return cur;
}

function setPath(obj: ProfileConfig, path: string, value: unknown) {
  const parts = path.split(".");
  let cur: Record<string, unknown> = obj;
  parts.slice(0, -1).forEach((part) => {
    const next = cur[part];
    if (next === null || typeof next !== "object" || Array.isArray(next)) cur[part] = {};
    cur = cur[part] as Record<string, unknown>;
  });
  const last = parts[parts.length - 1] ?? path;
  cur[last] = value;
}

function deletePath(obj: ProfileConfig, path: string) {
  const parts = path.split(".");
  const parentPath = parts.slice(0, -1).join(".");
  const parent = parentPath ? getPath(obj, parentPath) : obj;
  if (parent && typeof parent === "object" && !Array.isArray(parent)) {
    Reflect.deleteProperty(parent, parts[parts.length - 1] ?? path);
    if (parentPath && Object.keys(parent).length === 0) deletePath(obj, parentPath);
  }
}

/** Read a config into form values for ``fields``. */
export function configToForm(config: ProfileConfig, fields: FieldSpec[]): FormState {
  const form: FormState = {};
  for (const f of fields) {
    const v = getPath(config, f.path);
    switch (f.kind) {
      case "bool":
        form[f.path] = v === true;
        break;
      case "list":
        form[f.path] = Array.isArray(v) ? v.map((x) => String(x)) : [];
        break;
      case "commands":
        form[f.path] = Array.isArray(v)
          ? v.filter(Array.isArray).map((c: unknown[]) => c.map((x) => String(x)))
          : [];
        break;
      case "map":
        form[f.path] =
          v && typeof v === "object" && !Array.isArray(v)
            ? Object.entries(v as Record<string, unknown>).map(([k, val]) => [k, String(val)])
            : [];
        break;
      default:
        form[f.path] =
          typeof v === "string" || typeof v === "number" || typeof v === "boolean" ? String(v) : "";
    }
  }
  return form;
}

/**
 * Write form values onto a copy of ``base``. Empty values remove the key so adapter and
 * engine defaults apply. Numbers that do not parse are kept as text so the server's
 * validation explains the problem instead of the value silently disappearing.
 */
export function formToConfig(
  form: FormState,
  fields: FieldSpec[],
  base: ProfileConfig = {},
): ProfileConfig {
  const out = structuredClone(base);
  for (const f of fields) {
    const v = form[f.path];
    deletePath(out, f.path);
    if (v === undefined) continue;
    switch (f.kind) {
      case "bool":
        if (v === true) setPath(out, f.path, true);
        break;
      case "list": {
        // Arguments are passed exactly as written (no trimming); other lists are names.
        const raw = v as string[];
        const items =
          f.path === "args" || f.path === "extra_args"
            ? raw.filter((s) => s !== "")
            : raw.map((s) => s.trim()).filter(Boolean);
        if (!items.length) break;
        if (f.path === "success_exit_codes") {
          setPath(
            out,
            f.path,
            items.map((s) => (Number.isInteger(Number(s)) ? Number(s) : s)),
          );
        } else setPath(out, f.path, items);
        break;
      }
      case "commands": {
        const cmds = (v as string[][]).filter((c) => c.some((a) => a.trim()));
        if (cmds.length) setPath(out, f.path, cmds);
        break;
      }
      case "map": {
        const entries = (v as [string, string][]).filter(([k]) => k.trim());
        if (entries.length)
          setPath(
            out,
            f.path,
            Object.fromEntries(entries.map(([k, val]) => [k.trim(), val.trim()])),
          );
        break;
      }
      case "number": {
        const s = typeof v === "string" ? v.trim() : "";
        if (!s) break;
        const n = Number(s);
        setPath(out, f.path, Number.isFinite(n) ? n : s);
        break;
      }
      default: {
        const s = typeof v === "string" ? v.trim() : "";
        if (s) setPath(out, f.path, s);
      }
    }
  }
  return out;
}

/** Key-level differences between two configs (for version history). */
export function configChanges(
  before: ProfileConfig,
  after: ProfileConfig,
): { key: string; before: unknown; after: unknown }[] {
  const keys = [...new Set([...Object.keys(before), ...Object.keys(after)])].sort();
  return keys
    .filter((k) => JSON.stringify(before[k]) !== JSON.stringify(after[k]))
    .map((k) => ({ key: k, before: before[k], after: after[k] }));
}
