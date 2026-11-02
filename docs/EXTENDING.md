# Extending AgentTape

This is the contract for everyone building on the core. The core is built
around **registries of files**: you add a module or a folder, and it is discovered
automatically. You should almost never need to edit a central list. When you must touch
a shared file, keep the change small and additive.

Paths below are relative to the repository root. Backend code lives in
`backend/src/agenttape/` (written `agenttape/` below).

- [Ground rules](#ground-rules)
- [Where things live](#where-things-live)
- [Services to call](#services-to-call)
- [Backend extension points](#backend-extension-points)
  - [API router](#add-an-api-router) · [CLI command](#add-a-cli-command) ·
    [Agent adapter](#add-an-agent-adapter) · [Evaluator](#add-an-evaluator) ·
    [Run executor](#add-a-run-executor-workflows) · [Queue job](#add-a-queue-job-that-is-not-a-run) ·
    [Workflow node type](#add-a-workflow-node-type) · [Background service](#add-a-background-service-scheduler) ·
    [Worker hooks](#worker-hooks-git-isolation-budgets-workflow-wake-ups) ·
    [Approval handler](#approval-resolution-handlers) · [Demo seed](#add-demo-seed-data) ·
    [Tables and migrations](#tables-and-migrations)
- [Frontend extension points](#frontend-extension-points)
  - [Feature, route, nav item](#add-a-feature-route-and-nav-item) · [Run-detail tab](#add-a-run-detail-tab) ·
    [Run-detail header action](#add-a-run-detail-header-action) · [API calls](#call-the-api)
- [Tests](#tests)
- [The quality gate](#the-quality-gate)

## Ground rules

1. **One run engine.** Anything that starts agent work calls `RunService.create_run`.
   Do not insert `runs` rows yourself and do not execute adapters outside the worker.
2. **Status changes go through `application/transitions.py`** (`transition_run`,
   `transition_attempt`, `transition_step`). They validate against the domain tables and
   use conditional updates so races fail loudly instead of overwriting.
3. **Events go through `EventRecorder`** (services) or `ctx.events` (adapters). Both
   redact and cap. Never write `events` rows directly. Free-form columns (outputs,
   evidence, approval payloads, metadata, failure messages) are also redacted on every
   write by the guard in `security/persistence.py`; add new free-form columns to its
   `GUARDED` table.
4. **Never `shell=True`.** Launch processes with `adapters/process.run_process`.
5. **Never fake numbers.** Unknown usage, cost, exit codes, or judge results are
   `None` in the API and render with `<Unavailable />` in the UI.
6. **Demo data is tagged.** Anything a seed creates sets `is_demo=True`.
7. **No em dash (U+2014)** in any tracked file; `make check` fails on one.

## Where things live

| Concern | Location |
|---|---|
| Pure rules (statuses, transitions, failure categories, event types, prompt syntax, retry) | `agenttape/domain/` |
| Services (use cases) | `agenttape/application/services/` |
| DTOs shared by API, CLI, and UI types | `agenttape/application/schemas/` (add your own module) |
| Ports (queue) | `agenttape/application/ports.py` |
| ORM models | `agenttape/infrastructure/db/models.py`; new tables in `infrastructure/db/extra_models/` |
| Migrations | `agenttape/infrastructure/db/migrations/versions/` |
| Queue, leases, artifact store, clock | `agenttape/infrastructure/` |
| Adapters | `agenttape/adapters/` (`base.py`, `process.py`, `registry.py`, `builtin/`) |
| Evaluators | `agenttape/evaluations/` (`base.py`, `builtin/`) |
| Worker, executors, hooks, reconciliation | `agenttape/worker/` |
| Background services (`serve` supervisor) | `agenttape/runtime/` |
| Git snapshot and workspace interface | `agenttape/git/` |
| Redaction, paths, tokens, rate limiting, secrets | `agenttape/security/` |
| HTTP API | `agenttape/api/` (`app.py`, `routers/`) |
| CLI | `agenttape/cli/` (`main.py`, `commands/`) |
| Demo seeds | `agenttape/seeds/` |
| Web UI | `frontend/src/` (`features/`, `components/ui/`, `api/`, `app/`) |
| End-to-end tests | `e2e/tests/` |

`AppContext` (`application/context.py`) is the handle every entry point shares:
`settings`, `engine`, `session_factory`, `clock`, `queue`, `leases`, `artifact_store`,
`adapters`, `evaluators`, `notifier`, and `ctx.session()` (commit on success, rollback on
error). Build one with `agenttape.bootstrap.build_context(settings, migrate=True)`.

## Services to call

All services take an `AppContext` in their constructor.

**`agenttape.application.services.runs.RunService`**

```python
create_run(req: RunCreate, *, actor: str = "local user") -> RunCreateResult  # .run, .created
enqueue(session, run: Run, *, available_at: datetime | None = None) -> str   # joins caller's txn
cancel_run(run_id, *, reason: str | None = None, actor=...) -> RunOut
retry_run(run_id, *, reason=None, actor=...) -> RunOut                        # new attempt, same run
get_run(run_id) -> RunOut
get_detail(run_id) -> RunDetail
list_runs(*, status=None, source=None, project_id=None, workflow_id=None, schedule_id=None,
          parent_run_id=None, kind=None, search=None, sort="created_at", order="desc",
          limit=50, offset=0, ...) -> Page[RunOut]
list_events(run_id, *, after_sequence=0, limit=200, types=None, severities=None,
            attempt_id=None, node_key=None, text=None) -> EventPage
update_notes(run_id, *, notes, tags) -> RunOut
```

`RunCreate` (`application/schemas/runs.py`) covers every caller: `kind` (`"agent"` or
your executor's kind), `agent_profile_id` (+ optional `agent_profile_version_id`),
`prompt_template_id` (+ optional `prompt_version_id`) or inline `prompt`, `inputs`,
`project_id`, `priority`, `idempotency_key`, `source` (`RunSource.SCHEDULE`, `WEBHOOK`,
`WORKFLOW`, `REPLAY`, `FORK`, ...), `source_ref`, `evaluators`, `max_attempts`,
`timeout_seconds`, `idle_timeout_seconds`, `budget`, linkage (`parent_run_id`,
`parent_relation`, `parent_node_key`, `workflow_id`, `workflow_version_id`,
`schedule_id`, `trigger_id`), `extra_snapshot` (extra immutable sections, e.g. the
workflow definition), and `defer_enqueue` (create queued but let the caller enqueue).
Challenge markers are reserved: an `extra_snapshot["finding_review"]` section, and
`parent_relation="finding_review"` or `source=RunSource.REVIEW` without it, are refused
(422); only `FindingReviewService` creates challenge runs (see SECURITY.md T27).
Idempotency is enforced by `UNIQUE (source, idempotency_key)`; a duplicate returns the
existing run with `created=False` (HTTP 200 instead of 201). Reusing a key with a different request (the fields in
`runs.FINGERPRINT_FIELDS`) raises `IdempotencyConflictError` (409).

**Others**

| Service | Key methods |
|---|---|
| `services.prompts.PromptService` | `create_template(PromptTemplateCreate)`, `create_version(template_id, PromptVersionCreate)`, `get`, `list_versions`, `resolve_version(session, template_id, version_id)`, `preview(template_id, inputs, version_id=None)` |
| `services.profiles.AgentProfileService` | `create(AgentProfileCreate)`, `create_version(profile_id, AgentProfileVersionCreate)`, `validate_config(adapter_type, config) -> [ValidationIssueOut]`, `get`, `list_versions`, `list_profiles(...)`, `resolve_version(...)`, `adapters() -> [AdapterInfo]` |
| `services.projects.ProjectService` | `create(ProjectCreate)` (allowlisted canonical path), `update`, `get`, `list_projects`, `refresh_git_state` |
| `services.approvals.ApprovalService` | `request(session, *, workspace_id, title, rationale, type, risk_level, requested_action, run_id, attempt_id, step_run_id, node_key, resume_handler="run", expires_in_seconds=None, is_demo=False)`, `approve(id, reviewer=, note=)`, `reject(...)`, `expire_overdue(now=None)`, `get`, `list_approvals(status=, run_id=)` |
| `services.evaluations.EvaluationService` | `evaluate(spec, EvaluationContext)`, `persist(run_id=, attempt_id=, step_run_id=, spec=, outcome=, duration_ms=, evaluator_version=, node_key=)`, `loaders(run_id)` |
| `services.workflows.WorkflowService` | `create(WorkflowCreate)`, `create_version(id, WorkflowVersionCreate)`, `validate(definition)`, `get`, `list_workflows`, `list_versions`, `start_run(id, WorkflowRunCreate, source=, ...)` (see [ORCHESTRATION.md](ORCHESTRATION.md)) |
| `services.safety.SafetyApprovals` | typed approvals with action fingerprints: `request_commit`, `request_elevated_command`, `request_protected_path_write`, `request_budget_increase`, `request_evaluation_override`, `require_approved(session, id, type, action=)` |
| `services.evaluation_catalog.EvaluationCatalogService` | versioned definitions and suites, `resolve_specs`, `evaluate_run`, `rerun`, `human_score`, `compare(run_ids)` |
| `services.artifacts.ArtifactService` | `store(session, *, run_id, name, data, kind, media_type, attempt_id, step_run_id, metadata, redactor)`, `list_for_run`, `get`, `get_storage`, `read_text` |
| `services.exports.ExportService` | `run_bundle(run_id) -> dict` |
| `services.schedules.ScheduleService` | `create`, `update`, `set_enabled`, `delete`, `get`, `list_schedules`, `history`, `preview(SchedulePreviewRequest)`, `run_now(id, force=)`, `status()`, `targets()` |
| `services.triggers.TriggerService` | `create` (returns the token once), `update`, `rotate_token`, `delete`, `list_triggers`, `deliveries`, `test_mapping`, `handle_delivery(trigger_id, InboundRequest)` |
| `services.limits.LimitsService` | `get()`, `update(LimitsSettings)`, `for_run(run_id)` (effective budget, checks, retry policy, concurrency scopes) |
| `services.dashboard.DashboardService` | `summary(days=14) -> Dashboard` (live work, approvals, schedules, outcomes, durations, real vs simulated usage) |
| `services.demo.DemoService` | `seed(include_runs=True, execute_inline=True)`, `reset(actor=)`, `start_run(scenario=, source=, inputs=)` |
| `services.audit` | `record_audit(session, clock, *, action, resource_type, resource_id, actor, details, run_id, workspace_id)`, `list_audit(session, ...)` |
| `services.workspaces` | `ensure_default_workspace`, `default_workspace_id`, `allowed_roots(session, settings)`, `get_app_setting` / `set_app_setting` |
| `application.events.EventRecorder` | `append(session, run_id, drafts, redactor=None)`, `record(session, run_id, type, summary, payload, **draft_fields)` |

Record an audit entry for every material action (approvals, cancellations, config
changes, commits, deletions).

## Backend extension points

### Add an API router

Create `agenttape/api/routers/<name>.py` exposing `router = APIRouter(tags=["<name>"])`.
It is mounted under `/api/v1` automatically. Paths in the router are relative
(`@router.get("/schedules")`).

- Dependencies: `from agenttape.api.deps import Ctx, Paging, Actor`.
- Return pydantic models from `application/schemas/`; lists return `Page[T]` built with
  `application.services.common.paginate(...)`.
- Raise `agenttape.domain.errors` exceptions (`NotFoundError`, `ValidationError`,
  `ConflictError`, `PolicyViolationError`); the app turns them into
  `{"error": {"code", "message", "details"}}` with the right status.
- Endpoints that authenticate differently (webhooks with their own hashed tokens) list
  their prefixes in a module-level `PUBLIC_PATHS = ["/webhooks/"]`, relative to `/api/v1`.
  They must enforce their own auth and rate limit (`security/ratelimit.RateLimiter`), and
  their size cap (the global body cap does not apply to public paths).
- Keep handlers thin: parse, call a service, return.

### Add a CLI command

Create `agenttape/cli/commands/<name>.py` with:

```python
import typer
from agenttape.cli.common import call, get_context, print_table, echo_json

def register(app: typer.Typer) -> None:
    schedules = typer.Typer(help="Manage schedules.", no_args_is_help=True)
    app.add_typer(schedules, name="schedules")

    @schedules.command("list")
    def list_(as_json: bool = typer.Option(False, "--json")) -> None:
        ctx = get_context()
        page = call(lambda: ScheduleService(ctx).list_schedules())
        ...
```

`call(...)` turns `AgentTapeError` into a clean message and exit code 1. Commands must
call the same services as the API.

### Add an agent adapter

Create `agenttape/adapters/builtin/<name>.py`:

```python
from agenttape.adapters.base import AgentAdapter, AdapterResult, DiscoveryResult, ExecutionContext, ProfileIssue
from agenttape.adapters.process import CommandSpec, run_process
from agenttape.adapters.registry import register_adapter

@register_adapter
class CodexAdapter(AgentAdapter):
    name = "codex-cli"
    display_name = "Codex CLI"
    capabilities = frozenset({"structured_events", "usage"})

    def discover(self) -> DiscoveryResult: ...          # never raise, never need credentials
    def validate_profile(self, config) -> list[ProfileIssue]: ...
    def normalize(self, raw: dict) -> list[EventDraft]: ...   # one JSON record -> events
    async def execute(self, ctx: ExecutionContext) -> AdapterResult:
        spec = CommandSpec(argv=[exe, "exec", "--json", ...], cwd=ctx.workdir,
                           env=ctx.build_env(ctx.profile.get("env_allowlist", [])),
                           stdin=(ctx.resolved_prompt() or "").encode())
        async def on_line(line: str) -> bool:      # structured stdout
            for draft in self.normalize(json.loads(line)):
                await ctx.events.emit(draft)
            return True
        result = await run_process(spec, ctx, on_stdout=on_line)
        ...
```

What the `ExecutionContext` gives you: `run` (id, snapshot...), `attempt` (id, number,
max), `profile` (the snapshotted config), `prompt` (stored text with `[secret:NAME]`
markers), `resolved_prompt()` (real values; never persist it), `inputs`, `workdir`,
`events` (`EventEmitter`: `emit`, `event`, `touch`), `cancel` (`CancellationToken`:
`cancelled`, `sleep`, `wait`), `artifacts` (`write_text` / `write_bytes`), `files`
(`record([FileChangeDraft])`), `resume_state` (checkpoint after an approval),
`resolve_secret(name)` (also registers the value for redaction), `redact`, `build_env`,
`log_dir`, `data_dir`, `limits`.

`run_process` enforces cancellation and timeouts (the worker cancels the token), process
group kill, concurrent draining, raw log files, and redaction. Return
`AdapterResult.success(...)`, `AdapterResult.failure(FailureCategory.X, message, ...)`, or
`AdapterResult(outcome="needs_approval", approval=ApprovalSpec(...), checkpoint={...})`.
Report usage/cost only when the tool reports it. Document the adapter in
`docs/integrations/<name>.md`. See `adapters/builtin/demo.py` for a complete example.

### Add an evaluator

Create `agenttape/evaluations/builtin/<type>.py`:

```python
@register_evaluator
class RegexEvaluator(Evaluator):
    type = "regex"
    version = "1"
    def validate_config(self, config) -> list[str]: ...
    async def evaluate(self, ctx: EvaluationContext, config) -> EvaluationOutcome:
        events = await ctx.load_events(["stdout"])
        ...
        return EvaluationOutcome(status=EvaluationStatus.PASS, explanation="...", evidence={...})
```

Configured evaluators (`profile.config["evaluators"]` or `RunCreate.evaluators`, each
`{"type", "name", "config", "required"}`) run after every attempt; a failed required
evaluation fails the run with `evaluation_failed`. `EvaluationService.evaluate` turns
exceptions into an `error` outcome. Definitions, versions, and suites have tables already
(`evaluation_definitions`, `evaluation_definition_versions`, `evaluation_suites`).

### Add a run executor (workflows)

`Run.kind` selects the executor. Create `agenttape/worker/executors/<kind>.py`:

```python
@register_executor
class WorkflowExecutor(RunExecutor):
    kind = "workflow"
    async def execute(self, runtime: WorkerRuntime, claim: ClaimedItem) -> None:
        ...  # must always complete the queue item: runtime.ctx.queue.complete(claim.id, runtime.worker_id)
```

Create workflow runs with `RunService.create_run(RunCreate(kind="workflow", workflow_id=...,
workflow_version_id=..., extra_snapshot={"workflow": definition}, ...))`. Persist node
state in `step_runs` (unique per run and node key) using `transition_step`; child agent
runs are ordinary runs created with `parent_run_id`, `parent_relation="workflow_node"`,
and `parent_node_key`. Do not hold a worker slot while children run: complete your queue
item, and re-enqueue the parent (`RunService.enqueue`) from an `on_run_terminal` hook when
a child finishes. See `worker/executors/agent.py` for the lifecycle, watchdogs, and
finalisation pattern.

### Add a queue job that is not a run

Not every queue item is a run. A job with no `Run` behind it at all (`citation_reproduction`
is the example: re-running a citation's recorded argv) is enqueued with `run_id=None` so run
concurrency limits and run-queue reconciliation never mistake it for one, and a `run_kind` in
its payload names which executor claims it:

```python
ctx.queue.enqueue(
    session,
    dedupe_key=f"reproduction:{row.id}",
    run_id=None,
    kind="run",
    priority=0,
    payload={"run_kind": REPRODUCTION_KIND, "reproduction_id": row.id},
)
```

`Worker._execute` checks `claim.payload["run_kind"]` before falling back to `Run.kind`, so
register the executor the same way as a run executor; its `kind` is the `run_kind` string,
not a `Run.kind`:

```python
@register_executor
class CitationReproductionExecutor(RunExecutor):
    kind = REPRODUCTION_KIND
    async def execute(self, runtime: WorkerRuntime, claim: ClaimedItem) -> None:
        try:
            ...  # do the work; never raise past this method
        finally:
            await asyncio.to_thread(runtime.ctx.queue.complete, claim.id, runtime.worker_id)
```

Always complete the queue item, in a `finally`, exactly like a run executor. An interrupted
job (a worker crash or kill) leaves a visible `"running"` row on its own table rather than
retrying silently; a fresh request creates a new row instead. See
`agenttape/worker/executors/citation_reproduction.py` and
`agenttape/application/services/reproductions.py`.

A job that needs an isolated Git checkout to do its work (`citation_reproduction` re-runs a
command in one) reuses the exact same worktree shape a run's own Git isolation uses: a
directory named by an opaque key directly under `WorktreeManager`'s managed root
(`self.root / key`), created and torn down through the same guarded Git runner and the same
path-containment check (`WorktreeManager.owns_path`), with no separate git-policy code path
for "a job's worktree" versus "a run's worktree". The one difference is that a reproduction's
snapshot is always detached (`git worktree add --detach`, no branch), so there is never a
branch for AgentTape's V1 safety policy to consider deleting.

### Add a workflow node type

Node executors are a registry too. Create `agenttape/workflows/nodes/<type>.py`:

```python
@register_node
class MyNode(NodeExecutor):
    type = NodeType.MY_NODE
    inline = True  # False for a node observed on later ticks (a child run, an approval)
    async def start(self, nc: NodeContext) -> NodeOutcome:
        ...  # read nc.config and nc.template_context; return NodeOutcome(...) or NodeOutcome.fail(...)
```

`load_node_executors()` imports every module in `agenttape/workflows/nodes/`, so the module
needs no registration elsewhere (`workflows/nodes/finding_review.py` is a deferred example,
`transform.py` an inline one). A node type still needs a few central edits, because the
definition is validated before any executor runs:

- `agenttape/domain/workflows.py`: a `NodeType` value, its `NODE_TYPE_LABELS` entry, and a
  branch in `validate_node_config` (plus the graph checks in `validate_definition` if the
  node refers to other nodes, as `finding_review` does with `target`);
- `agenttape/application/services/workflows.py`: its `NODE_TYPE_HELP` entry (the editor's
  help text and config fields) and, if its config names profiles, prompts, or evaluation
  definitions, a branch in `_reference_issues`;
- the frontend: the `NodeType` union and the `NODE_TYPES` list in
  `features/workflows/api.ts`; TypeScript then flags every `Record<NodeType, ...>` map
  that needs an entry (`TYPE_LABELS` and `TYPE_HINTS` in `graph.ts`, `TYPE_ICONS` in
  `WorkflowGraph.tsx`), and `defaultConfig` in `graph.ts` and the config form in
  `NodeForm.tsx` need a case.

### Add a background service (scheduler)

Create `agenttape/runtime/services/<name>.py`:

```python
class SchedulerLoop:
    name = "scheduler"
    def __init__(self, ctx: AppContext) -> None: ...
    async def run(self, stop: asyncio.Event) -> None: ...   # return promptly when stop is set

register_service(ServiceSpec(name="scheduler", factory=SchedulerLoop, description="..."))
```

The scheduler is one such service: `agenttape/runtime/services/scheduler.py`,
`agenttape/scheduler/` (`calc.py`, `policies.py`, `engine.py`), and `ScheduleService`; see
[SCHEDULING.md](SCHEDULING.md). Webhook trigger sources are a registry too
(`agenttape/triggers/`, see [TRIGGERS.md](TRIGGERS.md#adding-a-trigger-source-jira-github-app)).

`agenttape serve` starts every registered service with `default_enabled=True`
(`--no-scheduler` / `--without NAME` disable one) and restarts it with backoff if it
crashes. The registered services are `worker`, `scheduler`, `workflows` (re-wakes stalled
workflow runs and expires overdue approvals), and `observed-sessions` (closes idle observed
Claude Code runs). Add a standalone command (e.g. `agenttape scheduler`) as a CLI module. For
single-owner loops use `ctx.leases.acquire("scheduler", owner_id, ttl)`.

### Worker hooks (Git isolation, budgets, workflow wake-ups)

`agenttape/worker/hooks.py`; register from any module that is imported at startup (an
executor, adapter, or runtime service module):

| Hook | Signature | Use |
|---|---|---|
| `register_workdir_preparer(fn)` | `async (ctx, WorkdirRequest) -> PreparedWorkdir` | choose/prepare the directory an attempt runs in (Git worktree isolation). Last registered wins. |
| `register_after_attempt(fn)` | `async (ctx, ExecutionContext, AdapterResult) -> AdapterResult` | capture diffs and file changes, enforce budgets (return a failure with `BUDGET_EXCEEDED`), run validation commands. |
| `register_on_run_terminal(fn)` | `async (ctx, run_id, status) -> None` | react when a run finishes or pauses (wake a workflow parent, update schedule history). |
| `register_attempt_monitor(fn)` | `async (ctx, ExecutionContext, system_events) -> None` | a task that runs for the attempt's lifetime next to the watchdogs and is cancelled when it ends; stop the attempt with `exec_ctx.cancel.cancel(category, message)`. Emit on `system_events` (source `agenttape`), which does not count as agent activity. Budgets use this. |

`register_after_attempt(fn, order=50)` runs hooks by ascending `order` (ties keep
registration order). Budget checks register with `order=90` so they see file changes and
artifacts recorded by earlier hooks (diff capture should keep the default or go lower).

**Queue claim guards.** `agenttape.infrastructure.queue.register_claim_guard(fn)` adds a
check `(session, leased QueueItem) -> reason | None` that runs after a claim commits; a
reason sends the item back to the queue for about a second. Concurrency limits use this
(`agenttape/limits/concurrency.py`). The limits worker hooks and claim guard are
registered by `install()` in `agenttape/limits/__init__.py`; the module
`agenttape/worker/executors/limits.py` only calls it, so they are installed whenever a
worker loads its executors.

### Approval resolution handlers

`ApprovalRequest.resume_handler` names what happens on a decision. Built in: `"run"`
(approve re-queues a run in `waiting_for_approval`, which resumes the paused attempt from
its checkpoint; reject or expiry fails it with `approval_rejected` / `approval_expired`)
and `"none"`. Add your own:

```python
from agenttape.application.services.approvals import register_approval_handler

def resume_workflow_node(ctx, session, approval, approved: bool) -> None: ...
register_approval_handler("workflow_node", resume_workflow_node)
```

Handlers run inside the decision's transaction. Every decision already writes an audit
entry and an `approval_*` event. `expire_overdue()` is called by the worker's maintenance
loop; a scheduler may call it too.

### Add demo seed data

Create `agenttape/seeds/<name>.py` with `ORDER = <int>` (core data is 10) and
`def seed(ctx, report: SeedReport) -> None`. Look records up by name first so seeding is
idempotent, tag everything `is_demo=True`, and use services rather than raw inserts.
`report.data` carries ids from earlier seeds (`project_id`, `profiles`, `prompts`).
`reset-demo` deletes demo rows from **every table that has an `is_demo` column**, so give
new top-level tables that column.

### Tables and migrations

- New tables go in a new module in `agenttape/infrastructure/db/extra_models/`, using
  `Base`, `IdMixin`, `TimestampMixin`, `UTCDateTime`, and the `JSONType` column types from
  `infrastructure/db/base.py`. Use opaque UUID string ids, UTC timestamps, explicit
  `ondelete`, and unique constraints for anything a race could duplicate.
- Prefer the JSON columns core tables already have before adding columns to them.
- Queries must work on SQLite and PostgreSQL.

**Migrations.** The history starts linear: `0001_core` (the core schema), then
`trackE_0001` (`run_workspaces`). M2 branched three parallel heads off
`trackE_0001` (`m2A_0001` runtime sessions, `m2B_0002` file evidence, `m2C_0001` context
evidence); `m3_0001` merged them back into one ancestor, and `m3F_0001` (findings,
citations, citation verdicts) follows it. Two branches then grew off `m3F_0001`:
`m3C_0001` (`observed_sessions`) and the verification branch (`m3V_0001`:
`evidence_blobs`, `evidence_reads`, `citation_reproductions`, then `m3V_0002`: the
one-pending-reproduction-per-citation index). `m3_0002` merges `m3C_0001` and `m3V_0002`,
so the schema has one head again. Add a revision with `down_revision` set to the current
head (`cd backend && uv run alembic -c alembic.ini heads` prints it):

- bring a scratch database to head (`cd backend && uv run alembic -c alembic.ini upgrade
  heads`), change the models, then `uv run alembic -c alembic.ini revision
  --autogenerate -m "..." --rev-id <prefix>_NNNN`, and review the file (autogenerate misses
  some changes, and SQLite needs batch operations, which the environment enables);
- name the file after its revision id, e.g. `versions/runnotes_0001_add_pinned.py`;
- if two branches of work add revisions in parallel, both chain from the same head and a
  merge revision (`alembic merge heads`) reunites them. Until then the schema has several
  heads, which still migrates: `agenttape migrate`, `serve`, `init`, and the tests always
  upgrade to `heads`.

`make check` runs the migration smoke test (upgrade, downgrade to base, upgrade, then a
drift check against the models), so every downgrade must work.

## Frontend extension points

The UI is React 19 + TypeScript (strict) + TanStack Query + React Router + Tailwind v4.
Import shared pieces with the `@/` alias.

### Add a feature, route, and nav item

Each feature is a folder `frontend/src/features/<name>/` with a `feature.tsx`:

```tsx
import { CalendarClock } from "lucide-react";
import type { FeatureManifest } from "@/app/registry";

export const feature: FeatureManifest = {
  id: "schedules",
  nav: { label: "Schedules", icon: CalendarClock, order: 40, path: "/schedules" },
  routes: [
    { path: "schedules", element: <SchedulesPage /> },
    { path: "schedules/:scheduleId", element: <ScheduleDetailPage /> },
  ],
};
```

Features are collected with `import.meta.glob` in `src/app/registry.ts`. Nav order:
Overview 10, Findings 15, Runs 20, Workflows 30, Schedules 40, Prompts 50, Agents 60,
Projects 70, File Activity 75, Context 76, Approvals 80, Settings 90. Pages outside the
main nav (webhook triggers, limits, retention, onboarding, compare) register routes
without a `nav` entry and are linked from Settings or the page that uses them. A new section adds a folder; a new settings page
adds a tile to `SETTINGS_LINKS` in `features/settings/SettingsPage.tsx`.

Build pages with `components/layout/Page` and the shared UI kit in
`components/ui/` (`Button`, `IconButton`, `Card`, `StatusPill`, `Badge`, `EmptyState`,
`Unavailable`, `DataTable` with server sorting and pagination, `Tabs`, `ConfirmDialog`,
`useToast`, `Skeleton`/`LoadingBlock`, `ErrorState`, `CodeBlock`, `JsonInspector`,
`RelativeTime`, `Duration`). Use the design tokens (`bg-surface`, `text-muted`,
`border-line`, `text-accent`, `var(--st-<status>)`...), never raw hex values. Keep list
state (filters, sort, page) in the URL like `features/runs/RunsPage.tsx`. Confirm
material actions with `ConfirmDialog`. Empty states offer the next action; unknown values
render `<Unavailable />`.

### Add a run-detail tab

Create `frontend/src/features/<name>/runDetailTabs.tsx`:

```tsx
import type { RunDetailTab } from "@/app/registry";

export const runDetailTabs: RunDetailTab[] = [
  {
    id: "workflow",
    label: "Workflow",
    order: 20,
    isAvailable: (detail) => detail.run.kind === "workflow",
    count: (detail) => detail.steps.length,
    render: ({ detail, events, live }) => <WorkflowGraph detail={detail} />,
  },
];
```

Tab orders: Timeline 10, Findings 12, Workflow 20, Attempts 30, Runtime 35, Inputs and
snapshot 40, Files 45, Lineage 45, Context 47, Changes 50, Artifacts 60, Evaluations 70,
Limits 75. A tab with the same `id` as an existing one replaces it (the workflows feature
replaces `evaluations` this way). The URL keeps the selected tab in `?tab=<id>`. `count`
returns a number or a node: a count that needs a hook (for example a query-backed count)
returns a component, which should render nothing while the value is unknown, as
`features/findings/runDetailTabs.tsx` does.

### Add a run-detail header action

Create `frontend/src/features/<name>/runDetailActions.tsx` exporting
`runDetailActions: RunDetailAction[]` (`{ id, order, render: ({ detail }) => ReactNode }`).
Actions render after the core Cancel/Retry/Export controls, ordered by `order`; the same
`id` in a later module replaces an earlier one. The findings feature adds Challenge
findings (`features/findings/runDetailActions.tsx`, order 5) and the lineage feature adds
Replay, Fork, and Compare (`features/lineage/runDetailActions.tsx`, order 10).

### Call the API

The typed client is hand-written (`frontend/src/api/`): `client.ts` (`api<T>(path, {method,
query, body})`, `ApiError` from the error envelope, token header), `types.ts` (mirrors the
backend DTOs; keep it in sync, `agenttape openapi` prints the schema), `hooks.ts` (TanStack
Query hooks and `queryKeys`), and `sse.ts` (fetch-based SSE with `Last-Event-ID` resume).
Put feature-specific hooks and types next to your feature.

## Tests

Write tests with the code.

- Backend unit tests: `backend/tests/unit/`. Integration tests: `backend/tests/integration/`.
  Fixtures in `backend/tests/conftest.py`: `settings` and `ctx` (a fresh temp data dir and
  migrated SQLite database per test), `client` (FastAPI `TestClient`), and helpers
  `demo_profile(ctx, scenario, **config)`, `simple_prompt(ctx)`, `start_run(ctx, profile_id)`,
  `drive(ctx, run_id)` (executes queued work in-process until the run settles), and
  `event_types(ctx, run_id)`. Tests never touch `~/.agenttape`.
- Build credential-shaped test strings by concatenation (`"AKIA" + "..."`) so the secret
  scan stays clean.
- Shared backend test helpers: `backend/tests/integration/conftest.py` (`repo_project`, a
  project backed by a fresh Git repository), `tests/integration/helpers.py` (`git`,
  `make_repo_project`, `race_two_contexts`, and the findings event-record builders), and
  `tests/integration/capture_helpers.py` (hook payload builders and MCP helpers). Reuse
  them instead of copying a builder into a new test file.
- Frontend unit tests: `*.test.ts(x)` next to the code (Vitest + Testing Library;
  `src/test/fixtures.ts` has the event, run-detail, and finding factories: `makeFinding`,
  `makeCitation`, `makeVerdict`, `makeAdapter`).
- End-to-end: `e2e/tests/*.spec.ts` (Playwright). `playwright.config.ts` starts
  `scripts/e2e-server.sh`, which serves the built UI on a fresh temporary data dir with
  seeded demo data. Run `make e2e`.

## The quality gate

`make check` runs `scripts/check.sh`, in this order: no em dash (U+2014) in any tracked file (`git grep`,
binary files skipped), backend dependency sync, ruff format and lint, mypy (strict), pytest, the
migration smoke test, frontend dependency sync, eslint, prettier, the frontend `tsc
--noEmit`, the e2e `tsc --noEmit`, vitest, the production build, the secret scan, the
container config static checks, `docker build` (reported `SKIPPED-EXTERNAL` when Docker is
not installed), and Playwright. It prints a summary and fails on any failure. A
gate that cannot run because of the environment is reported as `SKIPPED-EXTERNAL`, never
as passed. Run it before you hand work back.
