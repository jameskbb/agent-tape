/**
 * Hand-written types mirroring the backend DTOs in
 * `backend/src/agenttape/application/schemas/`. Keep in sync when a schema changes;
 * `agenttape openapi` prints the authoritative OpenAPI document.
 */

export type RunStatus =
  | "queued"
  | "preparing"
  | "waiting_for_approval"
  | "running"
  | "cancelling"
  | "cancelled"
  | "succeeded"
  | "failed"
  | "timed_out"
  | "skipped";

export const RUN_STATUSES: RunStatus[] = [
  "queued",
  "preparing",
  "waiting_for_approval",
  "running",
  "cancelling",
  "cancelled",
  "succeeded",
  "failed",
  "timed_out",
  "skipped",
];

export const TERMINAL_STATUSES: ReadonlySet<RunStatus> = new Set([
  "cancelled",
  "succeeded",
  "failed",
  "timed_out",
  "skipped",
]);

export type Severity = "debug" | "info" | "notice" | "warning" | "error" | "critical";

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
  next_offset: number | null;
}

export type Usage = Record<string, number | boolean | string> & { simulated?: boolean };

export interface Run {
  id: string;
  workspace_id: string;
  project_id: string | null;
  title: string;
  kind: string;
  source: string;
  source_ref: string | null;
  idempotency_key: string | null;
  status: RunStatus;
  failure_category: string | null;
  failure_message: string | null;
  priority: number;
  adapter_type: string | null;
  prompt_template_id: string | null;
  prompt_version_id: string | null;
  agent_profile_id: string | null;
  agent_profile_version_id: string | null;
  workflow_id: string | null;
  workflow_version_id: string | null;
  schedule_id: string | null;
  trigger_id: string | null;
  parent_run_id: string | null;
  parent_relation: string | null;
  parent_node_key: string | null;
  tags: string[];
  notes: string | null;
  max_attempts: number;
  attempt_count: number;
  queued_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  cancel_requested_at: string | null;
  usage: Usage | null;
  cost: Record<string, unknown> | null;
  is_demo: boolean;
  created_at: string;
  updated_at: string;
  created_by: string | null;
  queue_position: number | null;
  prompt_name: string | null;
  agent_profile_name: string | null;
  project_name: string | null;
  /** How this run's own agent records findings (`RunOut.findings_capture`): an adapter
   * hosting several agents, such as Herdr, answers per run. Null when the run has no agent
   * profile or its adapter is no longer installed. */
  findings_capture: FindingsCapture | null;
}

/** How one run records findings: the AgentTape MCP server, a deterministic script, or not
 * at all. */
export type FindingsCapture = "mcp" | "scripted" | "unsupported";

export interface Attempt {
  id: string;
  run_id: string;
  attempt_number: number;
  status: RunStatus;
  failure_category: string | null;
  failure_message: string | null;
  worker_id: string | null;
  exit_code: number | null;
  workdir: string | null;
  stdout_bytes: number;
  stderr_bytes: number;
  outputs: Record<string, unknown> | null;
  usage: Usage | null;
  cost: Record<string, unknown> | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  heartbeat_at: string | null;
  retry_scheduled_at: string | null;
  has_checkpoint: boolean;
}

export interface StepRun {
  id: string;
  run_id: string;
  node_key: string;
  node_type: string;
  name: string;
  status: RunStatus;
  position: number;
  attempt_count: number;
  child_run_id: string | null;
  approval_request_id: string | null;
  inputs: Record<string, unknown> | null;
  outputs: Record<string, unknown> | null;
  failure_category: string | null;
  failure_message: string | null;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
}

export interface RunEvent {
  id: string;
  run_id: string;
  attempt_id: string | null;
  step_run_id: string | null;
  node_key: string | null;
  parent_event_id: string | null;
  sequence: number;
  ts: string;
  monotonic_ms: number;
  type: string;
  source: string;
  severity: Severity;
  summary: string;
  payload: Record<string, unknown>;
  redaction: { redacted: boolean; count: number; kinds: string[] } | null;
  usage: Usage | null;
  cost: Record<string, unknown> | null;
  schema_version: number;
}

export interface EventPage {
  items: RunEvent[];
  next_after_sequence: number | null;
  last_sequence: number;
  total: number;
}

export interface Artifact {
  id: string;
  run_id: string;
  attempt_id: string | null;
  step_run_id: string | null;
  name: string;
  kind: string;
  media_type: string;
  size_bytes: number;
  sha256: string;
  is_text: boolean;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface FileChange {
  id: string;
  run_id: string;
  attempt_id: string | null;
  path: string;
  old_path: string | null;
  change_type: "added" | "modified" | "deleted" | "renamed";
  insertions: number | null;
  deletions: number | null;
  is_binary: boolean;
  size_bytes: number | null;
  is_protected: boolean;
  diff_artifact_id: string | null;
  created_at: string;
}

export type EvaluationStatus = "pass" | "fail" | "warning" | "error" | "skipped";

export interface EvaluationResult {
  id: string;
  run_id: string;
  attempt_id: string | null;
  step_run_id: string | null;
  definition_id: string | null;
  definition_version_id: string | null;
  name: string;
  evaluator_type: string;
  evaluator_version: string;
  status: EvaluationStatus;
  required: boolean;
  score: number | null;
  evidence: Record<string, unknown>;
  explanation: string | null;
  duration_ms: number | null;
  created_at: string;
}

export interface Approval {
  id: string;
  workspace_id: string;
  run_id: string | null;
  attempt_id: string | null;
  step_run_id: string | null;
  node_key: string | null;
  type: string;
  title: string;
  rationale: string | null;
  risk_level: "low" | "medium" | "high" | "critical";
  requested_action: Record<string, unknown>;
  status: "pending" | "approved" | "rejected" | "expired" | "cancelled";
  resume_handler: string;
  requester: string;
  reviewer: string | null;
  reviewer_note: string | null;
  expires_at: string | null;
  resolved_at: string | null;
  is_demo: boolean;
  created_at: string;
  updated_at: string;
}

export interface RunDetail {
  run: Run;
  snapshot: Record<string, unknown>;
  inputs: Record<string, unknown>;
  rendered_prompt: string | null;
  outputs: Record<string, unknown> | null;
  options: Record<string, unknown>;
  attempts: Attempt[];
  steps: StepRun[];
  artifacts: Artifact[];
  file_changes: FileChange[];
  evaluations: EvaluationResult[];
  approvals: Approval[];
  children: Run[];
  event_count: number;
  last_sequence: number;
}

export interface RunCreateResult {
  run: Run;
  created: boolean;
}

export interface DemoScenario {
  scenario: string;
  profile_name: string;
  description: string;
}

export interface SystemInfo {
  version: string;
  data_dir: string;
  database: string;
  host: string;
  port: number;
  token_required: boolean;
  allowed_roots: string[];
  adapters: string[];
  evaluators: string[];
  limits: Record<string, number>;
}

export interface AdapterInfo {
  name: string;
  display_name: string;
  description: string;
  capabilities: string[];
  status: "installed" | "unavailable" | "misconfigured" | "ready";
  version: string | null;
  executable: string | null;
  message: string | null;
}

export interface ApiErrorBody {
  error: { code: string; message: string; details: Record<string, unknown> };
}
