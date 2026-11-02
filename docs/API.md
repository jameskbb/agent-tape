# HTTP API

AgentTape's REST API is versioned under `/api/v1`. The web UI and the CLI use the same
application services, so anything the UI does is available here. The live, authoritative
schema is the OpenAPI document served by a running instance:

- interactive docs: `http://127.0.0.1:8787/api/docs`
- OpenAPI JSON: `http://127.0.0.1:8787/api/openapi.json`, or offline with
  `uv run --project backend agenttape openapi -o openapi.json`

## Conventions

| Topic | Rule |
|---|---|
| Authentication | None on loopback by default. When AgentTape binds beyond loopback (or `AGENTTAPE_REQUIRE_TOKEN=true`), every endpoint except `/health` and `/ready` needs `Authorization: Bearer <token>` (or `X-AgentTape-Token`). Webhook delivery uses each trigger's own token instead. |
| Errors | Always `{"error": {"code": "...", "message": "...", "details": {...}}}` with a matching status: `400` bad request, `401` unauthorized, `403` policy violation, `404` not found, `409` conflict or invalid transition, `413` payload too large, `415` wrong content type, `422` validation error, `429` rate limited, `500` internal error, `503` token auth required but not configured. |
| Pagination | List endpoints take `limit` (1-200, default 50) and `offset`, and return `{"items", "total", "limit", "offset", "next_offset"}` (`next_offset` is null on the last page). Events use a cursor instead: `after_sequence` and `next_after_sequence`. |
| Idempotency | `POST /runs` and `POST /workflows/{id}/runs` accept an `Idempotency-Key` header (or `idempotency_key` in the body). A repeat of the same request returns the existing run with status `200` and `"created": false`; the first returns `201`. Reusing a key for a *different* request (different prompt, inputs, profile, project, or limits) returns `409 idempotency_key_conflict` with the existing run id in `details`. Keys are unique per source. |
| Timestamps | ISO 8601 in UTC. Schedules also carry an IANA time zone for display and next-run math. |
| Unknown values | `null`, never `0`: token usage, cost, exit codes, and durations that were not reported stay null. Demo usage carries `"simulated": true`. |
| Size limits | Request bodies 2 MB (webhooks 4 MB and a per-trigger cap). Event payloads are capped; full output is in the raw log download. |

## Examples

Start a Demo Agent run and follow it:

```bash
API=http://127.0.0.1:8787/api/v1
RUN=$(curl -s -X POST $API/demo/runs -H 'Content-Type: application/json' \
  -d '{"scenario": "success"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["run"]["id"])')
curl -N $API/runs/$RUN/events/stream          # Server-Sent Events until the run ends
curl -s "$API/runs/$RUN/events?type=tool_call&limit=20"
curl -s $API/runs/$RUN/export -o run.json     # sanitized bundle
```

Create a run from a saved prompt and agent profile, idempotently:

```bash
curl -s -X POST $API/runs -H 'Content-Type: application/json' -H 'Idempotency-Key: ticket-4821' \
  -d '{"agent_profile_id": "<profile id>", "prompt_template_id": "<prompt id>",
       "inputs": {"goal": "Validate empty names"}, "source": "api"}'
```

Resume a live stream after a disconnect: send `Last-Event-ID: <last sequence>` (or
`?after_sequence=`). The stream sends `event: run_event` messages with `id: <sequence>`,
a comment ping every 15 seconds, and `event: end` once the run is finished and every event
has been delivered.

Deliver a webhook (see [TRIGGERS.md](TRIGGERS.md) for input mapping and GitHub examples):

```bash
curl -s -X POST $API/webhooks/<trigger id> -H "Authorization: Bearer $TRIGGER_TOKEN" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: delivery-1' -d '{"goal": "..."}'
```

## Endpoints

Paths are relative to `/api/v1`. Request and response models are in the OpenAPI document.

### System

| Method | Path | What it does |
|---|---|---|
| GET | `/health` | Liveness probe |
| GET | `/ready` | Readiness probe (database reachable, migrations applied, data dir present) |
| GET | `/system/info` | Runtime information (never includes secrets) |

### Runs and events

| Method | Path | What it does |
|---|---|---|
| GET | `/runs` | List runs with filters, search, and sorting |
| POST | `/runs` | Create a run (idempotent with Idempotency-Key) |
| GET | `/runs/{run_id}` | Run detail with attempts, snapshot, artifacts |
| PATCH | `/runs/{run_id}` | Update run notes and tags |
| POST | `/runs/{run_id}/cancel` | Cancel a run |
| GET | `/runs/{run_id}/events` | Paginated, filterable events |
| GET | `/runs/{run_id}/events/stream` | Live events over Server-Sent Events (resume with Last-Event-ID or after_sequence) |
| GET | `/runs/{run_id}/export` | Portable, sanitized JSON bundle of a run |
| GET | `/runs/{run_id}/logs/{attempt_number}/{stream}` | Download a raw (redacted) attempt log |
| POST | `/runs/{run_id}/retry` | Retry a failed run (new attempt) |

### Artifacts

| Method | Path | What it does |
|---|---|---|
| GET | `/artifacts/{artifact_id}` | Get Artifact |
| GET | `/artifacts/{artifact_id}/content` | Artifact bytes. Paths come only from the database and are confined to the store. |
| GET | `/runs/{run_id}/artifacts` | List Run Artifacts |

### Replay, fork, compare, export

| Method | Path | What it does |
|---|---|---|
| GET | `/runs/{run_id}/compare/{other_id}` | Compare two runs side by side |
| GET | `/runs/{run_id}/export/archive` | Sanitized zip bundle: bundle.json plus the run's text artifacts |
| POST | `/runs/{run_id}/fork` | Fork a run with a changed prompt version, agent profile, or variables |
| GET | `/runs/{run_id}/lineage` | Parent and child runs, environment drift, and fork options |
| POST | `/runs/{run_id}/replay` | Replay a run with its exact versions and inputs |

### Git changes and commits

| Method | Path | What it does |
|---|---|---|
| GET | `/runs/{run_id}/git` | Starting revision, worktree, changed files with parsed diff, validations, commits |
| POST | `/runs/{run_id}/git/cleanup` | Remove the run's AgentTape-owned worktree if it has no uncommitted work |
| POST | `/runs/{run_id}/git/commit` | Request a local commit in the run's isolated worktree (needs an approval) |

### Runtime (Herdr)

See [HERDR.md](HERDR.md). Responses never include socket paths or native session values.

| Method | Path | What it does |
|---|---|---|
| GET | `/runs/{run_id}/runtime` | The run's runtime references (Herdr workspace, pane, agent) and attach command |
| GET | `/runtime/herdr` | Herdr installed/reachable, version, protocol, capabilities, machines, integrations |
| POST | `/runtime/herdr/test` | Test the Herdr connection and return actionable diagnostics |

### File evidence

See [FILE-EVIDENCE.md](FILE-EVIDENCE.md) for the model and terminology. Ledger filters
(`classification`, `operation`, `net_status`, `source` repeatable; `step`, `agent`,
`protected`, `outside`, `generated`, `net_unchanged`, `q`, `sort`, `order`)
apply to both the list and the ledger export.

| Method | Path | What it does |
|---|---|---|
| GET | `/runs/{run_id}/files` | The run's file ledger: one row per logical file, filtered, searched, sorted, paginated |
| GET | `/runs/{run_id}/files/summary` | Counts (null when unavailable), completeness statement, collapsed noise, protected and boundary findings |
| GET | `/runs/{run_id}/files/timeline` | One file's (`?path=`) evidence trail, start/end state, final diff, linked events and approvals |
| GET | `/runs/{run_id}/files/export` | Sanitized ledger export of the current filter (`format=csv\|json`) |
| POST | `/runs/{run_id}/files/rebuild` | Rebuild summaries from stored evidence; `reconcile=true` re-reads the workdir's Git state (audited; 409 until the run has finished) |
| GET | `/runs/{run_id}/evidence/export` | Sanitized evidence bundle (`format=json`) or its raw evidence events (`format=csv`) |
| GET | `/projects/{project_id}/file-activity` | Most touched, failed-run, changed-together, protected, and recent files (linked to runs) |
| GET | `/projects/{project_id}/file-activity/history` | One file's (`?path=`) run history in the project |

### Budgets and limits

| Method | Path | What it does |
|---|---|---|
| GET | `/runs/{run_id}/limits` | Effective budget, budget checks, timeouts, retry policy, and concurrency scopes |
| GET | `/settings/limits` | Global limits and live usage |
| PUT | `/settings/limits` | Update global limits |

### Approvals

| Method | Path | What it does |
|---|---|---|
| GET | `/approvals` | List Approvals |
| GET | `/approvals/{approval_id}` | Get Approval |
| POST | `/approvals/{approval_id}/approve` | Approve |
| POST | `/approvals/{approval_id}/reject` | Reject |

### Safety policy

| Method | Path | What it does |
|---|---|---|
| GET | `/policy` | The V1 safety policy |
| POST | `/policy/check-command` | Would this argument array be allowed, need approval, or be refused? |

### Evaluations

| Method | Path | What it does |
|---|---|---|
| GET | `/evaluation-definitions` | List Definitions |
| POST | `/evaluation-definitions` | Create Definition |
| GET | `/evaluation-definitions/{definition_id}` | Get Definition |
| PATCH | `/evaluation-definitions/{definition_id}` | Update Definition |
| GET | `/evaluation-definitions/{definition_id}/versions` | List Definition Versions |
| POST | `/evaluation-definitions/{definition_id}/versions` | Create Definition Version |
| GET | `/evaluation-suites` | List Suites |
| POST | `/evaluation-suites` | Create Suite |
| GET | `/evaluation-suites/{suite_id}` | Get Suite |
| PATCH | `/evaluation-suites/{suite_id}` | Update Suite |
| GET | `/evaluations/compare` | Compare evaluation results across runs (2-6 runs) |
| GET | `/evaluator-types` | Evaluator types with availability (agent-as-judge is unavailable unless configured) |
| GET | `/evaluators` | Registered evaluator types |
| GET | `/runs/{run_id}/evaluations` | Run Evaluations |
| POST | `/runs/{run_id}/evaluations/human` | Record a person's score and notes |
| POST | `/runs/{run_id}/evaluations/rerun` | Rerun deterministic evaluations against the run's recorded data |
| POST | `/runs/{run_id}/evaluations/run` | Evaluate a run now with definitions, a suite, or inline evaluators |

### Workflows

| Method | Path | What it does |
|---|---|---|
| GET | `/workflow-node-types` | List Node Types |
| GET | `/workflows` | List Workflows |
| POST | `/workflows` | Create Workflow |
| POST | `/workflows/validate` | Validate a definition (acyclic, known node types, references) without saving |
| GET | `/workflows/{workflow_id}` | Get Workflow |
| PATCH | `/workflows/{workflow_id}` | Update Workflow |
| POST | `/workflows/{workflow_id}/runs` | Start a workflow run (idempotent with Idempotency-Key) |
| GET | `/workflows/{workflow_id}/versions` | List Versions |
| POST | `/workflows/{workflow_id}/versions` | Save a new immutable version |
| GET | `/workflows/{workflow_id}/versions/{version_id}` | Get Version |

### Prompts

| Method | Path | What it does |
|---|---|---|
| GET | `/prompt-tags` | Every tag used by a prompt |
| GET | `/prompts` | List Prompts |
| POST | `/prompts` | Create Prompt |
| POST | `/prompts/export` | Export prompts as JSON |
| POST | `/prompts/import` | Import an export bundle; existing prompts are never overwritten |
| GET | `/prompts/{template_id}` | Get Prompt |
| PATCH | `/prompts/{template_id}` | Update Prompt |
| GET | `/prompts/{template_id}/diff` | Diff Versions |
| POST | `/prompts/{template_id}/duplicate` | Duplicate Prompt |
| POST | `/prompts/{template_id}/render` | Preview a rendered prompt (secret references appear as [secret:NAME]) |
| POST | `/prompts/{template_id}/test-run` | Run this prompt with the Demo Agent through the normal run engine |
| GET | `/prompts/{template_id}/versions` | List Versions |
| POST | `/prompts/{template_id}/versions` | Create Version |
| POST | `/prompts/{template_id}/versions/save` | Save an edit as a new version (409 when nothing changed) |
| GET | `/prompts/{template_id}/versions/{version_id}` | Get Version |
| PUT | `/prompts/{template_id}/versions/{version_id}` | Amend a version no run has used (409 version_immutable otherwise) |

### Agent profiles, adapters, secrets, executables

| Method | Path | What it does |
|---|---|---|
| GET | `/adapters` | Installed adapters and their status |
| GET | `/agent-profiles` | List Profiles |
| POST | `/agent-profiles` | Create Profile |
| POST | `/agent-profiles/validate` | Validate a profile configuration without saving it |
| GET | `/agent-profiles/{profile_id}` | Get Profile |
| PATCH | `/agent-profiles/{profile_id}` | Update Profile |
| GET | `/agent-profiles/{profile_id}/versions` | List Profile Versions |
| POST | `/agent-profiles/{profile_id}/versions` | Create Profile Version |
| GET | `/secret-references` | Secret reference metadata (values are never returned) |
| POST | `/secret-references` | Create Secret |
| DELETE | `/secret-references/{secret_id}` | Delete Secret |
| PATCH | `/secret-references/{secret_id}` | Update Secret |
| GET | `/settings/allowed-executables` | Get Executables |
| PUT | `/settings/allowed-executables` | Put Executables |
| POST | `/settings/allowed-executables/check` | Resolve an executable and report whether it may run |

### Projects, allowed roots, onboarding

| Method | Path | What it does |
|---|---|---|
| GET | `/onboarding` | Onboarding State |
| POST | `/onboarding` | Update Onboarding |
| GET | `/projects` | List Projects |
| POST | `/projects` | Create Project |
| POST | `/projects/check-path` | Canonicalize a path and explain whether it can become a project |
| GET | `/projects/{project_id}` | Get Project |
| PATCH | `/projects/{project_id}` | Update Project |
| GET | `/projects/{project_id}/activity` | Project Activity |
| POST | `/projects/{project_id}/refresh-git` | Refresh Git |
| GET | `/settings/allowed-roots` | Get Allowed Roots |
| PUT | `/settings/allowed-roots` | Put Allowed Roots |

### Schedules and scheduler

| Method | Path | What it does |
|---|---|---|
| GET | `/scheduler/status` | Scheduler owner and health |
| GET | `/schedules` | List schedules |
| POST | `/schedules` | Create Schedule |
| POST | `/schedules/preview` | Next run times for a draft schedule (DST-aware, in its time zone) |
| GET | `/schedules/targets` | Prompts, agent profiles, projects, and workflows a schedule or trigger can use |
| GET | `/schedules/timezones` | IANA time zone names |
| DELETE | `/schedules/{schedule_id}` | Delete Schedule |
| GET | `/schedules/{schedule_id}` | Get Schedule |
| PATCH | `/schedules/{schedule_id}` | Update Schedule |
| GET | `/schedules/{schedule_id}/history` | Occurrences: created, queued, skipped, missed, replaced, failed |
| POST | `/schedules/{schedule_id}/pause` | Pause Schedule |
| POST | `/schedules/{schedule_id}/resume` | Resume Schedule |
| POST | `/schedules/{schedule_id}/run-now` | Create an occurrence now (respects the concurrency policy unless force) |

### Webhook triggers

| Method | Path | What it does |
|---|---|---|
| GET | `/triggers` | List webhook triggers |
| POST | `/triggers` | Create a trigger. The token is returned once and stored only as a hash. |
| DELETE | `/triggers/{trigger_id}` | Delete Trigger |
| GET | `/triggers/{trigger_id}` | Get Trigger |
| PATCH | `/triggers/{trigger_id}` | Update Trigger |
| GET | `/triggers/{trigger_id}/deliveries` | Delivery log: accepted, duplicate, rejected, error |
| POST | `/triggers/{trigger_id}/rotate-token` | Replace the token (the old one stops working immediately) |
| POST | `/triggers/{trigger_id}/test-mapping` | Apply the input mapping to a sample payload without creating a run |

### Webhook delivery (public, token per trigger)

| Method | Path | What it does |
|---|---|---|
| POST | `/webhooks/{trigger_id}` | Deliver a webhook (authenticated with the trigger's own token) |

### MCP capture (public, loopback only, capture token)

| Method | Path | What it does |
|---|---|---|
| POST | `/mcp/findings` | Record a finding from the AgentTape MCP server (loopback callers only). Authenticated by a per-attempt capture token (with `X-AgentTape-Attempt`) for a run AgentTape launched, or by the per-machine `mcp` token with `cwd` and `instance_id` (and an optional `session_id`) for an interactive or Herdr-hosted session. 201 when recorded; 409 for an ambiguous session (`ambiguous_session`), a `session_id` this caller cannot use (`session_mismatch`), or a cancelled observation (`observation_cancelled`); 422 when `cwd` is outside every registered project (`outside_projects`); 429 `rate_limited` with `Retry-After`; for an interactive or Herdr-hosted session, 503 with `Retry-After` when the finding was not recorded because hook ingest was busy (`ingest_busy`), or when it may have been recorded but could not be confirmed in time (`finding_write_pending`). Recording the identical finding again within a minute returns the earlier event instead of a copy |

### Claude Code hooks (public, loopback only, capture token)

| Method | Path | What it does |
|---|---|---|
| POST | `/hooks/claude-code` | Receive a Claude Code http hook (observational only; always answers {}) |

### Dashboard

| Method | Path | What it does |
|---|---|---|
| GET | `/dashboard` | Overview: live work, outcomes, activity, and a `findings` block (recorded, all-quotes-match, declared-only, citations, verdict counts, unchecked) from the same cross-run read model as `GET /findings` |

### Retention

| Method | Path | What it does |
|---|---|---|
| POST | `/retention/execute` | Delete previewed runs and orphans that are still eligible (AgentTape data only) |
| POST | `/retention/preview` | Dry run: what cleanup would delete and how many bytes (nothing is deleted) |
| GET | `/retention/settings` | Get Settings |
| PUT | `/retention/settings` | Put Settings |

### Audit log

| Method | Path | What it does |
|---|---|---|
| GET | `/audit` | Get Audit |

### Demo data

| Method | Path | What it does |
|---|---|---|
| POST | `/demo/reset` | Delete demo-tagged records only |
| POST | `/demo/runs` | Start a live Demo Agent run |
| GET | `/demo/scenarios` | Scenarios |
| POST | `/demo/seed` | Create demo records (idempotent) |


### Context evidence

See [CONTEXT-EVIDENCE.md](CONTEXT-EVIDENCE.md) for stages, packs, and the learning loop.

| Method | Path | What it does |
|---|---|---|
| GET | `/runs/{run_id}/context` | Context summary for a run |
| GET | `/runs/{run_id}/context/packs` | The run's immutable context packs |
| GET | `/runs/{run_id}/context/evidence` | Provenance explorer: filtered, searchable, chronological context evidence |
| GET | `/runs/{run_id}/context/sources/{source_id}` | Source detail: safe locator, freshness, access history, linked findings |
| GET | `/runs/{run_id}/context/findings` | Findings with their supporting, conflicting, and missing sources, their citations, and each citation's latest verdict and check count, plus its latest reproduction (trigger, mode, status, result, reason, pinned commit) and reproduction count |
| POST | `/runs/{run_id}/findings/verify` | Re-check a finished run's citations against AgentTape's recording; appends verdicts, never overwrites (409 `run_not_finished` while the run is unfinished) |
| POST | `/runs/{run_id}/challenge` | Challenge a finished run's findings with a chosen agent profile: an ordinary child run independently re-derives them; its outcomes are model judgment, never proof (409 `run_not_finished` while the target run is unfinished) |
| GET | `/runs/{run_id}/challenges` | Challenges of a run's findings (on demand and workflow-node), each labelled model judgment, never proof |
| GET | `/citations/{citation_id}/content` | Numbered lines from the retained read a file citation was checked against, scoped to its own run, never the live file or the agent's quote. `context` (default 3) lines around the cited range, or `before`/`after` separately (each 0-200); at most 800 lines are returned, and `clipped` is true when that cap withheld a cited line (or, for a citation with no range, the rest of a longer read) |
| POST | `/citations/{citation_id}/reproductions` | Re-run the command a citation cites, as a queued job, through the command policy in an isolated snapshot worktree; the result never changes the citation's verdict. 202 with the queued reproduction; 422 for a file citation (only an event citation can be re-run); 409 `run_not_finished` while the run is unfinished; 409 `conflict` while a re-run of that citation is still queued or running |
| POST | `/findings/{finding_id}/confirm` | A person confirms a finding (optional note, max 2000 characters); appends `finding.confirm` to the audit log and returns the confirmation, derived from that log, not a stored column; 404 unknown finding, 409 `run_not_finished` when the run has not finished, 409 `conflict` when already confirmed |
| POST | `/findings/{finding_id}/confirm/withdraw` | Withdraws a finding's confirmation; appends `finding.withdraw_confirmation` to the audit log (the earlier confirm row is never edited or deleted) and returns `confirmation: null`; the finding and its citation verdicts are unchanged; 404 unknown finding, 409 when not currently confirmed |
| GET | `/findings` | Findings across every run in a window (`days`, 1-90, default 14), flagged verdicts first by default (`sort=newest` orders by recorded time only); filters by `project_id`, `agent_profile_id`, `verdict` (findings with at least one citation whose latest verdict is that value, or `declared_only` for findings with no citation), `kind`, `source` (`launched`/`observed`), and a `q` text search; each row names its run (title, status, kind, agent, project). The same cross-run read model backs the `findings` block of `GET /dashboard`, so the inbox and the Overview tile always agree |
| GET | `/runs/{run_id}/context/compare/{other_id}` | How two runs' supplied and retrieved context differed |
| POST | `/runs/{run_id}/context/feedback` | Record a correction or lesson candidate on a run (pending review) |
| GET | `/projects/{project_id}/context/activity` | Project Context Activity: retrieval, citations, gaps, and approved lessons |
| GET | `/context/feedback` | Context feedback (gaps, corrections, lessons) |
| POST | `/context/feedback/{feedback_id}/classify` | Classify feedback |
| POST | `/context/feedback/{feedback_id}/propose` | Change the proposed destination or lesson text before approval |
| POST | `/context/feedback/{feedback_id}/approve` | Approve the proposal (a human decision; nothing is promoted automatically) |
| POST | `/context/feedback/{feedback_id}/reject` | Reject the feedback |
