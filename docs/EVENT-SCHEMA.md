# Event schema (version 1)

Every observable thing that happens during a run is one event in a single, ordered,
vendor-neutral stream. Adapters translate their tool's native output into these events;
the UI, exports, evaluators, and comparisons read only this schema. See ADR 0004.

## Envelope

| Field | Type | Meaning |
|---|---|---|
| `id` | string (UUID) | Event id. Adapters may choose it so later events can reference it. |
| `run_id` | string | The run. |
| `attempt_id` | string or null | The attempt that produced it (null for run-level events such as "Run queued"). |
| `step_run_id` | string or null | Workflow step, when emitted inside a workflow node. |
| `node_key` | string or null | Workflow node key. |
| `parent_event_id` | string or null | E.g. a `tool_result` points at its `tool_call`. |
| `sequence` | integer | 1, 2, 3... per run, gap-free, unique (`UNIQUE (run_id, sequence)`). The SSE `id`. |
| `ts` | ISO 8601 UTC | When it was persisted. |
| `monotonic_ms` | number | Milliseconds since the attempt started, from a monotonic clock in the worker; for run-level events, milliseconds since the run was created. |
| `type` | string | See [types](#types). |
| `source` | string | Adapter name (`demo`, `claude-code`...) or `agenttape` for engine events. |
| `severity` | string | `debug`, `info`, `notice`, `warning`, `error`, `critical`. |
| `summary` | string | One human-readable line, at most 500 characters, redacted. |
| `payload` | object | Type-specific data (below), redacted and size-capped. |
| `redaction` | object or null | `{"redacted": true, "count": n, "kinds": [...]}` when anything was removed. |
| `usage` | object or null | Token usage exactly as reported. Demo numbers carry `"simulated": true`. |
| `cost` | object or null | Cost exactly as reported; never estimated. |
| `schema_version` | integer | `1`. |

## Types

| Type | Emitted when | Typical payload |
|---|---|---|
| `lifecycle` | The run or attempt changes status | `status`, `attempt`, `worker_id`, `workdir`, `failure_category`, `message` |
| `agent_message` | The agent says something | `role`, `text` |
| `reasoning_summary` | The adapter explicitly provides a summary of its plan (never hidden chain-of-thought) | `text`, `provided_by` |
| `tool_call` | The agent invokes a tool | `tool`, `input`, `tool_use_id` |
| `tool_result` | A tool returns (`parent_event_id` = the call) | `tool_use_id`, `output`, `is_error`, `output_retained` (a read's output over the text cap; see "Size caps") |
| `command` | A process starts | `argv` (array), `cwd`, `pid`, `env_keys` (names only), `tool_use_id` (when the reporter gives one) |
| `stdout` / `stderr` | Output lines, batched | `text`, `lines`, `stream`, `truncated` |
| `file_read` / `file_write` | The agent reads or writes a file | `path`, `bytes`, `operation` |
| `git_change` | File changes were recorded | `files[]` (`path`, `change_type`, `insertions`, `deletions`, `is_binary`, `is_protected`), `total_files`, `insertions`, `deletions` |
| `git_change` (Git isolation actions) | An isolated worktree was created, a local commit was made after approval, or a worktree cleanup ran | `action` (`worktree_created`, `commit`, `worktree_cleanup`), `branch`, `path`, `base_commit`, `sha`, `message`, `approval_id`, `files[]` |
| `artifact` | An artifact was stored | `artifact_id`, `name`, `kind`, `size_bytes`, `sha256`, `media_type` |
| `usage` | The adapter reports usage | `input_tokens`, `output_tokens`, `simulated`, ... |
| `evaluation` | An evaluator finished | `evaluation_id`, `evaluator_type`, `status`, `required`, `explanation`, `evidence` |
| `approval_requested` / `approval_granted` / `approval_rejected` / `approval_expired` | Approval lifecycle | `approval_id`, `type`, `risk_level`, `rationale`, `requested_action`, `reviewer`, `note` |
| `retry` | A failed attempt will be retried, or a manual retry was requested | `failed_attempt`, `next_attempt`, `max_attempts`, `delay_seconds`, `failure_category`, `retry_at`, `manual` |
| `timeout` | A wall-clock or idle limit fired | `limit_seconds`, `kind` (`wall_clock` or `idle`) |
| `cancellation` | Cancellation was requested or completed | `reason`, `actor` |
| `error` | Something failed (adapter crash, reconciliation, spawn failure) | `error`, `traceback` (redacted), `attempt` |
| `warning` | Something degraded (e.g. stored-output budget reached) | varies; Git isolation sets `kind`: `dirty_worktree` (`head`, `branch`, `dirty_files`, `isolated`), `protected_path` (`files[]` with `path`, `patterns`), `replay_drift` (`of_run_id`, `drift[]`), `validation_skipped` |
| `scheduler` / `trigger` | Scheduling and webhook activity | varies |
| `step` | Workflow step changes | varies |
| `runtime` | AgentTape acted on an external runtime (Herdr start, prompt, wait, read, interrupt, reconnect) | `runtime`, `action`, sanitized refs |
| `file_evidence` | File evidence finding: protected path, outside boundary, collapsed noise, evidence gap | `path`, `finding` |
| `context` | Context provenance record (available, selected, retrieved, injected, referenced, cited, unavailable, stale, conflict, gap, correction) | `stage`, `source` |
| `finding` | Structured finding, recommendation, or decision with source links or citations | `id`, `kind`, `text`, `links`, optional `target` (adversarial review; see [Review findings](#review-findings)) |

New types are added to `domain/enums.EventType` and to this table.

### Finding citations

A `finding` event's `links` entries are one of three shapes: a source link
(`{source_id, relation}`, unchanged since M2), a file citation (`{path, line_start,
line_end, quote}`, optional `relation`), or an event citation (`{event_ref, quote}`,
optional `relation`). There is no separate citation event type: both citation shapes
travel inside the same `finding` payload as the legacy source links.

Citation-shaped links (a `path` or an `event_ref`) become rows in `findings` and
`citations` through `FindingsService`, the one writer; legacy source links are unchanged
and still become `EvidenceLink` rows only. An `event_ref` is resolved only within the same
run: either an AgentTape event id, or a `tool_use_id` carried by a `tool_result`, `command`,
or `tool_call` event of that run (a `tool_result` is preferred over a `command` over a
`tool_call` when more than one shares the ref, and the newest wins among ties); an
unresolved ref keeps the raw `event_ref` text with no `event_id`. Paths are resolved
lexically against the run's working directory (never opened, statted, or read); a path
outside it is recorded as `<outside>/<name>`. Caps recorded when applied: 50 citations per
finding (`citations_dropped` on the finding), 4000-character quotes (`quote_truncated` on
the citation), 200-character event refs, 4096-character paths (an over-limit or
NUL-containing path drops that citation and counts toward `citations_dropped`).

### Review findings

A reviewer challenging another run's findings (`POST /runs/{run_id}/challenge`,
`agenttape runs challenge`, or a `finding_review` workflow node) records its
verdict as an ordinary `finding` event with `kind: "review"` and a `target` object:
`{run_id, finding_key, outcome}`, where `outcome` is one of `upheld`, `refuted`, or
`inconclusive`, a vocabulary kept structurally separate from citation verdicts
(`quote_matches`, `stale`, ...): a review is the reviewer's own judgment on a claim, a
citation verdict is a mechanical check of a quote against what was actually read.

`FindingsService` stores this under `details.review` on the finding: `target_run_id`,
`target_finding_key`, `target_finding_id` (resolved once linked), `outcome`, `resolved`,
`reason`, `judgment: "model judgment"`, `counts_as_proof: false`. A review links to a
target finding only when the recording run is that target's own challenge (its snapshot
names the target run and the finding keys it was shown); otherwise `resolved` is `false`
and `reason` names why: `missing_target`, `invalid_target`, `invalid_outcome`,
`not_a_review_run`, `target_run_mismatch`, `not_presented`, or `unknown_finding`. A
review never writes or changes a citation verdict; the reviewer's own citations (on its
own finding) are ordinary citations, verified by the same automatic verification engine
as any other run's.

The reviewer is an ordinary `agent` run. Started on demand by Challenge findings (API,
CLI, or the run page), it is a child of the target run with run `source` `review`; started
by a `finding_review` workflow node, it is a child of the workflow run with run `source`
`workflow`.

## Capture sources (MCP and Claude Code hooks)

Two additional event sources feed the same event types above. Neither is a second event
schema, just two more places events come from:

- **`agenttape-mcp`**: a `finding` event recorded by `record_finding` (a per-attempt run
  AgentTape launched, or an interactive/Herdr-hosted session). `payload.recorded_via` is
  `"mcp"`; the optional `payload.target` (`{run_id, finding_key, outcome}`) passes through
  unchanged for an adversarial reviewer and is otherwise absent. `payload.capture_digest`
  is a sha256 of the rest of the payload and the sending MCP server's instance id: the same
  finding sent again by that server on the same attempt within 60 seconds (a retry after a
  timeout or a "may have been recorded" answer) returns the earlier event instead of
  recording a copy.
- **`claude-code-hook`**: `tool_call`/`tool_result` events recorded from Claude Code's own
  `http` hooks (`PreToolUse`/`PostToolUse`/`PostToolUseFailure`). A `tool_call` payload
  carries `cwd` (the hook's reported working directory) alongside `tool`/`input`/
  `tool_use_id`. A `tool_result` payload's `response_shape` is `read_file` (the same
  `cat -n` layout as a stream-json `Read` result), `bash` (with optional `stderr` and
  `interrupted`), `text`, `json`, `none`, or `error` (a `PostToolUseFailure`, which also
  carries `is_interrupt` when Claude Code reports it).

A run's `kind` is `observed` (alongside the existing `agent`/`workflow`) for a Claude Code
session AgentTape did not launch; its `source` is `hook` (Claude Code's own hooks
correlated it) or `mcp` (an interactive `record_finding` call created it with no
hook-correlated session in that directory). Two `lifecycle` phases bracket it:
`observed_started` (when AgentTape starts observing, with `session_start`, `cwd`, `model`)
and `observed_ended` (`SessionEnd`, an idle-window timeout, or the observation being
cancelled, with `status` and `closed_by`).

## Guarantees

- **Order**: `sequence` is allocated inside the writing transaction by atomically
  incrementing `runs.event_seq`, so concurrent writers (worker, API cancel, approval
  decisions) still produce a gap-free total order.
- **Redaction** before persistence: configured secret values and credential patterns are
  replaced with `[REDACTED:<kind>]`; values of sensitive keys (`password`, `token`,
  `api_key`, `authorization`...) become `[REDACTED:sensitive_field]`. Secret references
  appear as `[secret:NAME]`.
- **Size caps**: text fields over `AGENTTAPE_MAX_EVENT_TEXT_BYTES` (16 KB) are truncated
  with `<field>_truncated` and `<field>_original_bytes`; payloads over
  `AGENTTAPE_MAX_EVENT_PAYLOAD_BYTES` (64 KB) become `{"truncated": true, "preview": ...}`.
  After 2 MB of stored stdout (or stderr) per attempt, one `warning` event is recorded and
  further chunks live only in the raw log.
- **A read's output past the text cap**: when a `tool_result` answers a call File Evidence
  records as a read (a `Read`-style tool whose input names a path, or a `Bash` shell read)
  and its `output` is over the text cap (and, for a hook-recorded result, its
  `response_shape` is `read_file` or `bash`: an image or PDF read's JSON dump is never
  kept), the same redacted output, cut at 1 MiB on a line boundary, is also written
  to `artifacts/runs/<run_id>/evidence/<sha256>` (deleted with the run), and the event
  carries `output_retained`: `storage_path`, `sha256`, `bytes`, and `truncated` (true when
  the 1 MiB cut applied, or when the adapter reported the result itself as cut with
  `truncated` or `output_truncated`). Citation checks read that copy instead of the capped `output`
  (FINDINGS.md); the event row itself stays capped. Only AgentTape sets `output_retained`:
  a value an adapter or hook supplies is dropped, and the copy is always looked up by hash
  in the run's own evidence directory. This applies to Claude Code hooks and to launched
  runs; a launched Claude Code run's stream-json (stdout) lines reach the parser whole up
  to 8 MiB (its stderr lines, and other adapters' output lines, 64 KiB), and a longer line
  is split into `stdout` pieces whose tool result is not parsed.
- **Raw logs**: `logs/<run_id>/attempt-N.stdout.log` and `.stderr.log` hold the full
  redacted output up to `AGENTTAPE_MAX_RAW_LOG_BYTES` (50 MB per stream), downloadable at
  `GET /api/v1/runs/{id}/logs/{attempt}/{stdout|stderr}`.

## Reading events

- `GET /api/v1/runs/{id}/events?after_sequence=0&limit=200`: cursor page ordered by
  sequence; filters `type` (repeatable), `severity` (repeatable), `attempt_id`,
  `node_key`, `q` (case-insensitive summary search). Response: `items`,
  `next_after_sequence` (null on the last page), `last_sequence`, `total`.
- `GET /api/v1/runs/{id}/events/stream`: Server-Sent Events. Each event:

  ```
  id: 42
  event: run_event
  data: {"id": "...", "sequence": 42, "type": "tool_call", ...}
  ```

  Resume with the `Last-Event-ID` header or `?after_sequence=`. The server sends a
  comment ping every 15 seconds and, when the run is terminal and fully delivered,
  `event: end` with `{"status", "last_sequence"}`. Messages larger than
  `AGENTTAPE_MAX_SSE_MESSAGE_BYTES` carry `payload: {"truncated_for_stream": true}`;
  fetch the full event from the paginated endpoint.
- `GET /api/v1/runs/{id}/export`: a sanitized bundle with every event.
