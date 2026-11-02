# Architecture

AgentTape is a modular monolith (ADR 0001): one Python package with clear internal
boundaries, a React web UI served by the same process, and a database that doubles as
the work queue. This document covers the boundaries, the process model, and how a run
flows through the system. Extension points are described in
[EXTENDING.md](EXTENDING.md); the event schema in [EVENT-SCHEMA.md](EVENT-SCHEMA.md).

## Boundaries

```
             +-----------------------------+   +-------------------------+
  browser -> | api/  FastAPI (/api/v1, SSE)|   | cli/  Typer (agenttape) |
             +--------------+--------------+   +------------+------------+
                            |  thin transport; no business logic
                            v                                v
             +--------------------------------------------------------+
             | application/  services + schemas + ports                |
             |  RunService (the only run path), PromptService,         |
             |  AgentProfileService, ProjectService, ApprovalService,  |
             |  EvaluationService, ArtifactService, ExportService,     |
             |  DemoService, EventRecorder, transitions                |
             +------+--------------------+----------------------+------+
                    |                    |                      |
                    v                    v                      v
             +-------------+   +------------------+   +--------------------+
             | domain/     |   | infrastructure/  |   | security/          |
             | statuses,   |   | SQLAlchemy models|   | redaction, paths,  |
             | transitions,|   | + Alembic, DB    |   | tokens, rate limit,|
             | events,     |   | queue, leases,   |   | secret resolver    |
             | prompts,    |   | artifact store,  |   +--------------------+
             | retry       |   | clock            |
             +-------------+   +------------------+
                    ^
                    |  claims queue items
             +------+-----------------------------------------------+
             | worker/  Worker loop -> executor registry (by kind)   |
             |   AgentRunExecutor -> adapters/ registry -> adapter   |
             |   BatchingEventSink, recorders, hooks, reconciliation |
             +------------------------------------------------------+
             | runtime/  Supervisor for background services         |
             |   (worker, scheduler, workflows, observed-sessions)  |
             +------------------------------------------------------+
```

Feature packages built on this core (each registers through the extension points in
[EXTENDING.md](EXTENDING.md) rather than editing the engine):

| Package | What it adds | Deep doc |
|---|---|---|
| `adapters/builtin/` | Demo Agent, generic command, Claude Code, Codex CLI | [ADAPTERS.md](ADAPTERS.md), [integrations/](integrations/) |
| `workflows/` | DAG model, node types, durable engine (`worker/executors/workflow.py`) | [ORCHESTRATION.md](ORCHESTRATION.md) |
| `domain/policy.py`, `application/services/safety.py` | V1 safety policy and typed approvals | [ORCHESTRATION.md](ORCHESTRATION.md#5-approvals-and-the-safety-policy) |
| `evaluations/builtin/`, `application/services/evaluation_catalog.py` | evaluators, versioned definitions, suites, gates | [ORCHESTRATION.md](ORCHESTRATION.md#6-evaluations) |
| `scheduler/`, `runtime/services/scheduler.py` | lease-owned scheduler, DST-correct occurrences | [SCHEDULING.md](SCHEDULING.md) |
| `triggers/`, `api/routers/webhooks.py` | authenticated, idempotent webhook triggers | [TRIGGERS.md](TRIGGERS.md) |
| `limits/`, `worker/executors/limits.py` | budgets and concurrency limits (hooks and a queue claim guard) | [SCHEDULING.md](SCHEDULING.md#budgets) |
| `git/`, `worker/executors/git_workspace.py` | guarded Git runner, isolated worktrees, change capture, approved commits | ADR 0006, [SECURITY.md](SECURITY.md) |
| `application/services/lineage.py`, `comparison.py`, `retention.py` | replay, fork, compare, zip export, retention | [API.md](API.md) |
| `application/services/dashboard.py` | Overview aggregates (real vs simulated usage kept apart) | this page |
| `security/persistence.py` | redaction guard on every write of free-form columns | [SECURITY.md](SECURITY.md) |
| `herdr/`, `adapters/builtin/herdr.py`, `api/routers/runtime.py` | Herdr runtime: socket client, capability model, `runtime_sessions`, reattach after restart | [HERDR.md](HERDR.md), ADR 0009 |
| `file_evidence/`, `worker/executors/file_evidence.py`, `demo_agent.py` | file evidence: telemetry, watcher, Git reconciliation, ledger, exports; the evidence demo agent | [FILE-EVIDENCE.md](FILE-EVIDENCE.md) |
| `context_evidence/`, `worker/executors/context_evidence.py` | context packs, provenance events, finding links, reviewed lessons | [CONTEXT-EVIDENCE.md](CONTEXT-EVIDENCE.md) |
| `worker/evidence_feed.py` | one per-attempt reader of new events shared by file and context evidence | this page |
| `findings/`, `application/services/findings.py` | finding citations (file line range + quote, or event reference), the six-value verdict vocabulary, and `FindingsService`, the only reader and writer of findings, citations, and citation verdicts. In `findings/`, `normalize.py`, `citations.py`, `verify.py`, `review.py`, and `reproduce.py` are pure (no database, no I/O); `evidence.py` holds the run-scoped read and event lookups; `retention.py` is the only writer of `evidence_blobs` and `evidence_reads`; `retained_output.py` keeps a read's redacted output past the 16 KiB event text cap, up to 1 MiB, when `EventRecorder` records it, and that copy is what `retention.py` retains; `hooks.py` registers the run-terminal hook that verifies citations and queues automatic re-runs | [EVENT-SCHEMA.md](EVENT-SCHEMA.md#finding-citations), [CONTEXT-EVIDENCE.md](CONTEXT-EVIDENCE.md) |
| `worker/executors/findings.py` | Imports `findings/hooks.py` when the worker loads executors, so the run-terminal verification hook is registered; it registers no executor | this page |
| `application/services/verification.py` | `VerificationService`, the one application entry point that decides and records citation verdicts (`verify_run`, automatic on a run's terminal status and on `POST /runs/{run_id}/findings/verify`) | [CONTEXT-EVIDENCE.md](CONTEXT-EVIDENCE.md#citation-verification) |
| `application/services/reproductions.py`, `worker/executors/citation_reproduction.py` | `ReproductionService`, the one writer of `citation_reproductions` (`request`, `request_automatic`, `execute`); the `citation_reproduction` executor runs that queue job, which re-runs a citation's recorded argv in an isolated Git snapshot | [CONTEXT-EVIDENCE.md](CONTEXT-EVIDENCE.md#reproducing-cited-commands), [SECURITY.md](SECURITY.md) |
| `mcp/` | The AgentTape MCP server (`record_finding`, stdio transport): a thin loopback-HTTP caller that never opens the database or imports SQLAlchemy | [integrations/claude-code.md](integrations/claude-code.md#findings-over-mcp) |
| `capture/` | Pure mapping only: Claude Code hook payload parsing (`claude_hooks.py`) and the Claude Code `settings.json` hooks merge (`claude_settings.py`); no database, filesystem, or network access in this package | [integrations/claude-code.md](integrations/claude-code.md) |
| `cli/hook_relay.py` | The `SessionStart` command hook's own entry point, `python -I -m agenttape.cli.hook_relay` (Claude Code runs no `http` hook for SessionStart; `agenttape hooks relay` is a manual alias). Imports only the standard library and `security/client_token.py`. Posts the hook's stdin to the hooks endpoint with the token from the `0600` token store, fail-open inside a 3s deadline; the server takes it through the same path as an `http` hook | [integrations/claude-code.md](integrations/claude-code.md), [SECURITY.md](SECURITY.md) T30 |
| `api/capture_guard.py` | The error envelope, JSON content-type check, and capped body readers shared by the hooks, MCP, and webhook routers (the endpoints that skip the local API token and enforce their own limits) | [SECURITY.md](SECURITY.md) |
| `application/services/capture.py` | Writes the one `finding` event per `record_finding` call (per-attempt and interactive/Herdr-hosted paths), through the same `EventRecorder`/`FindingsService` path every other finding source uses | [EVENT-SCHEMA.md](EVENT-SCHEMA.md#capture-sources-mcp-and-claude-code-hooks) |
| `application/services/observed_sessions.py` | Correlates a Claude Code hook or an interactive `record_finding` call to a run: attach (AgentTape launched it), hosted (a Herdr pane), or a new `observed` run; `resolve_for_mcp` is the one MCP-side resolver | [integrations/claude-code.md](integrations/claude-code.md) |
| `runtime/services/observed_sessions.py` | The `observed-sessions` background service `serve` starts: it closes observed Claude Code runs after `AGENTTAPE_OBSERVED_IDLE_TIMEOUT_SECONDS` without hook activity (`agenttape serve --without observed-sessions` disables it) | [integrations/claude-code.md](integrations/claude-code.md) |
| `security/attempt_redactors.py` | In-memory, per-process registry of each running attempt's secret-value redactor, published by the worker so hook events and MCP findings recorded for the attempt are redacted with the same values (never persisted) | [SECURITY.md](SECURITY.md) |
| `security/capture.py` | Loopback-only rejection checks and the per-attempt/per-machine (`hooks`, `mcp`) capture token stores shared by the MCP and hooks routers | [SECURITY.md](SECURITY.md) |
| `findings/review.py` | Pure, no I/O: the reviewer prompt builder (nonce-delimited untrusted-data block, delimiter-text neutralization, caps) and the review vocabulary (`REVIEW_OUTCOMES`, kept structurally separate from `CITATION_VERDICTS`); `parse_review_target`/`review_details` parse and resolve a `target {run_id, finding_key, outcome}` | [EVENT-SCHEMA.md](EVENT-SCHEMA.md#review-findings), [SECURITY.md](SECURITY.md) |
| `application/services/finding_review.py` | `FindingReviewService.review_request`, the one request builder shared by the on-demand "Challenge findings" trigger (`start_challenge`) and the `finding_review` workflow node (`start_for_workflow_node`); `challenges` is the read model behind `GET /runs/{id}/challenges` | [API.md](API.md), [CONTEXT-EVIDENCE.md](CONTEXT-EVIDENCE.md) |
| `workflows/nodes/finding_review.py` | `FindingReviewNode`, a deferred node (`inline = False`) exactly like `agent_run`; resolves its target's child run and starts the review through `FindingReviewService`, never falling back to reviewing the workflow run itself | [ORCHESTRATION.md](ORCHESTRATION.md) |
| `adapters/builtin/_demo_review.py` | The Demo Agent's scripted, no-model `finding_review` reviewer, used by the review demo seed and by tests | [ADAPTERS.md](ADAPTERS.md) |
| `seeds/findings_demo.py`, `seeds/findings_review_demo.py` | Demo data for findings: the seeded evidence runs' verification (plus one labelled stand-in verdict) and one real on-demand challenge by the scripted reviewer | [CONTEXT-EVIDENCE.md](CONTEXT-EVIDENCE.md) |

Rules that keep the boundaries honest:

- `domain/` has no I/O and no framework imports beyond pydantic models for prompt
  variable definitions.
- API routers and CLI commands only parse input, call services, and format output.
- Services own transactions (`ctx.session()`), status transitions (always via
  `application/transitions.py`), events (via `EventRecorder`), and audit entries.
- Adapters never touch the database; they receive an `ExecutionContext`.

### Runtime and evidence boundary

Herdr owns runtime: terminals, workspaces, panes, agent processes, interactive sessions,
persistence, lifecycle detection, remote machines, and native session restoration.
AgentTape owns control and evidence. The `herdr` adapter is one more adapter behind the
same executor: it asks Herdr to start and prompt an agent, follows its lifecycle, and
records runtime references per attempt, so a Herdr run produces the same run record, file
evidence, context evidence, approvals, evaluations, and exports as a local run (an
integration test compares them). Around every attempt, whatever the runtime:

1. **Before the agent starts** the executor awaits `start_file_evidence`: Git start
   snapshot taken, filesystem watcher live. Context evidence assembles the attempt's
   immutable context pack and appends approved lessons to the prompt when the adapter
   receives it.
2. **During the attempt** one `EventFeed` per attempt reads new event rows about once a
   second and hands them to the file-evidence collector and the context normalizer, which
   append evidence rows idempotently (unique keys per source event).
3. **After the attempt** file evidence takes the Git end snapshot, reconciles it with the
   observed evidence, and rebuilds the per-run file ledger; context evidence rescans and
   rebuilds its summary. Both are rebuildable from the append-only rows.
4. **After an AgentTape restart** reconciliation re-queues an attempt whose Herdr session
   is still alive; the adapter reattaches, records a `runtime` reconnect event with the
   offline window, and both evidence systems mark that window incomplete instead of
   inventing events.

## Process model

| Command | What runs |
|---|---|
| `agenttape serve` | Uvicorn with the API and the built UI, plus every registered background service in the same event loop under a `Supervisor` (restart with backoff on crash). Applies migrations first unless `--no-migrate`. |
| `agenttape worker` | A standalone worker process (same code as the in-process one). |
| `agenttape scheduler` | A standalone scheduler. Only the holder of the `scheduler` database lease creates occurrences; others wait as standbys ([SCHEDULING.md](SCHEDULING.md)). |
| `agenttape demo`, `agenttape run --follow` | Create runs through `RunService`; if no worker heartbeat is live they execute the run in-process with an embedded worker. |

Several workers may run at once against the same database; the queue's leases make each
run's executor unique (ADR 0003). The worker runs attempts as asyncio tasks up to its
concurrency; database calls go through `asyncio.to_thread` so pipe draining never stalls.

The server binds `127.0.0.1:8787` by default. Binding beyond loopback is refused unless an
API token hash is configured; then every non-health endpoint requires
`Authorization: Bearer <token>` and failed attempts are rate-limited.

## Data flow of a run

1. **Create.** UI, API, CLI, schedule, webhook, or workflow calls
   `RunService.create_run`. It validates inputs against the prompt version's typed
   variables, renders the prompt with `[secret:NAME]` markers, captures Git state for the
   project, writes the immutable snapshot, locks the used versions, records a `queued`
   lifecycle event and an audit entry, and inserts a queue item, all in one transaction.
   An idempotency key returns the existing run instead.
2. **Claim.** A worker leases the queue item (conditional update), dispatches by
   `Run.kind` to an executor (`agent` and `workflow` built in; the `citation_reproduction`
   queue job, which is not a run, dispatches by its payload's kind).
3. **Prepare.** The executor moves the run `queued -> preparing`, creates attempt N (or
   reopens the attempt paused for approval), and asks the workdir preparer hook for a
   directory (project repo or scratch dir; Git isolation plugs in here).
4. **Run.** `preparing -> running`. The adapter executes with an `ExecutionContext`.
   Watchdogs enforce the wall-clock timeout, idle timeout, cancellation requests (polled
   from the database), and lease renewal. Subprocesses are drained, redacted, logged to
   raw files, and emitted as batched events.
5. **After.** After-attempt hooks run (diff capture, budgets), then configured evaluators;
   a failed required evaluation fails the run.
6. **Finalize.** Race-safe transitions to `succeeded`, `waiting_for_approval` (with an
   `ApprovalRequest` and the adapter checkpoint), `failed`, `timed_out`, or `cancelled`;
   retryable failures with attempts left go back to `queued` with exponential backoff.
   Terminal hooks fire, always through `worker/hooks.fire_run_terminal` (launched,
   workflow, observed, and cancelled runs alike).
7. **Observe.** The UI loads events page by page and follows
   `/runs/{id}/events/stream` (SSE with resume). Detail, artifacts, file changes,
   evaluations, attempts, and the snapshot come from `GET /runs/{id}`.
8. **Recover.** On start and every heartbeat interval, workers reconcile attempts whose
   lease expired as `interrupted`, expire overdue approvals, and reopen orphaned queue
   items.

## State machines

Runs, attempts, and workflow steps share one status vocabulary (queued, preparing,
waiting_for_approval, running, cancelling, cancelled, succeeded, failed, timed_out,
skipped) with separate transition tables in `domain/states.py`. Highlights:

- `running -> queued` is the automatic retry path; `failed | timed_out | cancelled ->
  queued` is the manual retry path (a new attempt on the same durable run).
- `waiting_for_approval -> queued` (approved) for runs and `waiting_for_approval ->
  running` for the resumed attempt.
- `succeeded` and `skipped` are final.

Failure categories (`domain/enums.FailureCategory`): adapter_error, timeout,
idle_timeout, cancelled, budget_exceeded, evaluation_failed, validation_failed,
approval_rejected, approval_expired, interrupted, policy_violation, config_error,
adapter_unavailable, dependency_failed, internal_error. Retried by default:
adapter_error, interrupted, idle_timeout, internal_error (profiles can override with
`retry.retry_on`).

## Storage

- Database (SQLite with WAL by default, PostgreSQL via `DATABASE_URL`; ADR 0002): all
  metadata, events, snapshots, and queue state. Complete model in
  `infrastructure/db/models.py` plus `infrastructure/db/extra_models/` (48 tables,
  including `run_workspaces` for Git isolation). PostgreSQL is supported by the SQL
  dialect rules in `models.py`. The quality gate runs only SQLite; the Python suite,
  including the `SELECT ... FOR UPDATE` path in `FindingsService.confirm`, passes when run
  by hand against PostgreSQL 17.
- Data directory (`AGENTTAPE_DATA_DIR`, default `~/.agenttape`):

  | Path | Contents |
  |---|---|
  | `agenttape.db` | SQLite database (plus WAL files) |
  | `artifacts/runs/<run_id>/` | artifact files, written atomically, SHA-256 recorded |
  | `artifacts/runs/<run_id>/evidence/` | retained read content that findings cite, and any read's output past the event text cap (redacted, capped at 1 MiB) |
  | `capture/` | capture token hashes (`tokens.json`), the `mcp` client token copy, and per-attempt `.mcp.json` launch configs, all owner-only |
  | `logs/<run_id>/attempt-N.{stdout,stderr}.log` | full redacted raw output, size-capped |
  | `fixtures/demo-repo/` | the Demo Agent's fixture repository |
  | `workspaces/` | AgentTape-owned working copies (demo clones, future worktrees) |
  | `api_token.sha256` | optional local API token hash |

## Web UI

Vite + React + TypeScript. `src/app/` holds the shell, theme, and registries; every
section is a folder in `src/features/` discovered by `import.meta.glob`. Shared components
live in `src/components/ui/`. The UI talks only to `/api/v1` through `src/api/`. In
production the build is copied into `agenttape/web_dist` and served with SPA fallback; in
development Vite proxies `/api` to the backend.
