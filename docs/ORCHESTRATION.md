# Orchestration: workflows, approvals, and evaluations

This document describes how AgentTape runs multi-step workflows, how durable approvals
pause and resume them, the V1 safety policy, and the evaluation system. Code lives in
`backend/src/agenttape/`:

| Concern | Location |
|---|---|
| DAG model, validation, templates, conditions (pure) | `domain/workflows.py` |
| Safety policy (pure) | `domain/policy.py` |
| Workflow definitions, versions, starting runs | `application/services/workflows.py` |
| The engine and its node types | `workflows/engine.py`, `workflows/nodes/*.py` |
| Run executor (`kind="workflow"`) | `worker/executors/workflow.py` |
| Maintenance sweep (lost wake-ups, approval expiry) | `workflows/maintenance.py`, `runtime/services/workflows.py` |
| Typed safety approvals (commit, elevated, ...) | `application/services/safety.py` |
| Evaluation definitions, suites, reruns, comparison | `application/services/evaluation_catalog.py` |
| Built-in evaluators | `evaluations/builtin/*.py` |
| HTTP API | `api/routers/workflows.py`, `evaluation_catalog.py`, `policy.py`, `approvals.py` |

## 1. The DAG model

A workflow has immutable **versions**. Each version is a definition:

```json
{
  "inputs":   [{"name": "goal", "type": "multiline", "default": "..."}],
  "settings": {"max_parallel": 3, "project_id": "..."},
  "nodes": [
    {"key": "prepare", "type": "command", "config": {"argv": ["git", "log", "--oneline", "-1"]}},
    {"key": "implement", "type": "agent_run", "config": {"agent_profile_id": "...", "prompt_template_id": "..."},
     "inputs": {"goal": "{{ inputs.goal }}"}, "timeout_seconds": 900}
  ],
  "edges": [{"from": "prepare", "to": "implement"}]
}
```

Saving always creates version N+1 (`POST /workflows/{id}/versions`); an unchanged
definition is refused with 409. A version is **locked** the first time a run uses it, and
every run copies the full definition into its immutable snapshot
(`snapshot.workflow.definition`), so editing a workflow never changes an earlier run.
Definitions are also normalized into `workflow_nodes` / `workflow_edges` rows.

### Node types

| Type | What it does | Outputs |
|---|---|---|
| `agent_run` | Runs an agent profile (template or inline prompt) as a **child run** through `RunService.create_run` (`parent_relation="workflow_node"`). Node `inputs` become the template's variables. | the child's outputs plus `child_run_id`, `status`, `workdir`, `failure_category`, `duration_ms` |
| `command` | One program with an argument array, never a shell. Policy-checked (section 5). `cwd` must be inside the project repository or an AgentTape-owned workspace. | `exit_code`, `stdout`, `stderr`, `stdout_lines`, `duration_ms`, `json` (with `parse_json`) |
| `transform` | Prompt render / data transform: `template` renders to `outputs.text`, `values` renders an object into the outputs. | as rendered |
| `evaluation_gate` | Runs evaluators (inline `evaluators`, `definition_ids`, and/or `suite_id`) against a `target` node's child run, or the workflow run. Gate evaluators are required unless `"required": false`. | `passed`, `failed`, `statuses`, `results`, `subject_run_id` |
| `approval` | Creates an approval request and pauses until it is decided. | `approved`, `reviewer`, `note`, `approval_id` |
| `condition` | Evaluates `expression`. Edges leaving it choose a branch. | `result`, `expression` |
| `collect` | Collects upstream outputs (`include`, default all ancestors) into the workflow result and a JSON artifact. | `collected`, `values`, `artifact_id` |
| `finding_review` | Challenges a `target` agent run node's findings with the chosen agent profile through `FindingReviewService`, the same builder the on-demand "Challenge findings" action uses. The child is an ordinary run with `parent_relation="workflow_node"`. Outcomes (upheld/refuted/inconclusive) are model judgment, recorded as findings on the child and never changing citation verdicts. | the reviewer's outputs plus `child_run_id`, `status`, `workdir`, `failure_category`, `duration_ms` |

Every node also has: `inputs` (templated), `input_schema` / `output_schema` (JSON Schema,
checked before start and after success), `timeout_seconds`, `max_attempts`,
`retry_backoff_seconds`, `required`, `continue_on_failure`, and an optional `position`
for the graph.

### Validation

`validate_definition` (and `POST /workflows/validate`) refuses a definition before it is
saved or run when: there are no nodes; a key is invalid or duplicated; an edge points at
an unknown node, at itself, or is duplicated; **the graph has a cycle** (the cycle is
named, e.g. `a -> b -> c -> a`); a node's configuration is incomplete; a branch edge does
not leave a condition node; an expression does not parse; or a template references an
unknown root, an undeclared input, or a node that is **not upstream** of the node using
it. The service additionally checks that referenced agent profiles, prompts, evaluation
definitions, suites, and projects exist. A `finding_review` node's `target` must additionally
name a real `agent_run` node upstream of it; at runtime a target with no child run or no
findings fails the node with a clear, non-retryable message rather than silently reviewing
the workflow run.

## 2. Template syntax

Templates are `{{ path }}` or `{{ path | filter }}` inside any string of a node's `inputs`
or `config`:

| Path | Value |
|---|---|
| `inputs.<name>` | a workflow run input (after defaults and type coercion) |
| `nodes.<key>.outputs.<field>...` | an upstream node's outputs; list indices are numbers (`files.0`) |
| `nodes.<key>.status` | an upstream node's status |
| `node.inputs.<name>` | the current node's resolved inputs (inside `config`) |
| `workflow.run_id`, `workflow.name`, `workflow.version`, `workflow.project_id` | the run |

Filters: `json` (serialize), `length`, `join` (comma-join a list).

Safety rules:

- A path is dotted identifiers and indices only. Lookup indexes dicts and lists; there is
  **no attribute access, no calls, and no evaluation** (`{{ inputs.x.upper() }}` and
  `{{ __class__ }}` are errors).
- Resolution is a single pass over the original text. A value that itself contains
  `{{ ... }}` is inserted literally and never expanded, so an agent's output cannot inject
  a template.
- A string that is exactly one template keeps the value's JSON type
  (`"{{ nodes.prep.outputs.files }}"` is a list); otherwise values are converted to text.
- A missing path is an error: the node fails with `config_error`.
- A command's executable (`argv[0]`) must be fixed text; only its arguments may be
  templated.

## 3. Scheduling, conditions, and failure

The engine starts every `queued` node whose upstream nodes are all settled, up to
`settings.max_parallel` at once (independent branches run in parallel). An edge is
**active** when its condition holds:

```json
{"from": "check", "to": "fix", "condition": {"on": "success", "branch": true, "when": "nodes.eval.outputs.passed == false"}}
```

- `on`: `success` (default), `failure` (the source failed or timed out), `always` (settled
  either way, but not skipped);
- `branch`: only for edges leaving a `condition` node, follows its `true` / `false` result;
- `when`: an expression that must also be true.

A node with no incoming edges runs. Otherwise it runs when **at least one** incoming edge
is active and is **skipped** when none are, so branches after a condition re-join, and
skips cascade down the graph.

The condition language is parsed by a small recursive-descent parser (never `eval`):
`==`, `!=`, `<`, `<=`, `>`, `>=`, `in`, `contains`, `and`, `or`, `not`, parentheses,
paths (as in templates, without braces), strings, numbers, `true`, `false`, `null`. A
missing path is `null`; comparing incompatible types is false.

**Failure.** A failed or timed-out node stops the workflow unless it is optional
(`required: false`), has `continue_on_failure`, or has an outgoing `failure`/`always`
edge. Stopping the workflow cancels running child runs and pending approvals, skips
queued nodes, and fails the run with the node's failure category (for example
`approval_rejected`, `evaluation_failed`, `policy_violation`). A workflow whose nodes all
settle without a fatal failure succeeds (optional failures are listed in the final
event).

**Retries and timeouts.** Inline nodes (`command`, `transform`, `condition`,
`evaluation_gate`, `collect`) retry up to `max_attempts` with exponential backoff from
`retry_backoff_seconds`; the run is re-queued for the retry time. Agent nodes pass
`max_attempts` and `timeout_seconds` to the child run, which retries through the normal
run engine. An approval node's `timeout_seconds` becomes the approval's expiry.

## 4. Durable execution and resume

The workflow run is an ordinary run (`kind="workflow"`); each node is a `step_runs` row
(unique per run and node key). The engine works in **ticks**: a worker claims the run's
queue item, the engine reads the persisted step state, observes child runs and
approvals, starts what is ready, runs inline nodes to completion, settles the run's
status, and completes the queue item. It never holds a worker slot while children run.

Ticks are triggered by:

| Trigger | Mechanism |
|---|---|
| Run created or retried | `RunService` enqueues it |
| A child run finishes or pauses | `on_run_terminal` hook (`worker/executors/workflow.py`) |
| An approval is decided or expires | the `workflow_node` approval handler (`application/services/workflows.py`) |
| The run (or a child) is cancelled | the cancel hook registered with `RunService` |
| A retry backoff elapses | the engine enqueues itself for the retry time |
| Anything lost in a crash | the maintenance sweep (every 2 s under `agenttape serve`) |

Why a restart never duplicates completed work:

- **Settled steps are final.** A `succeeded` / `failed` / `skipped` step is never started
  again (only an explicit retry of a failed workflow re-queues its unfinished steps, and
  keeps succeeded ones).
- **Child runs are idempotent.** An agent node's child is created with idempotency key
  `wf:<run>:<node>:<attempt>`. If the process dies after creating the child but before
  recording it, the next tick re-issues the start and gets the existing child back.
- **Approvals are rows.** A waiting step points at its approval request; the next tick
  reads the decision, whichever process made it.
- **Inline nodes are atomic from the engine's view.** A per-run named lease (`wf:<run>`)
  serializes ticks, so an inline step found `running` at the start of a tick belonged to
  a tick that died; it never completed, so it is re-queued (and says so in the timeline).
- **Lost wake-ups are recovered.** The sweep enqueues unfinished workflow runs that have
  work to do (a decided approval, a finished child, a ready node, an orphaned step) and no
  live queue item, and marks queue items abandoned by a dead worker as done.

The integration test `test_pause_restart_and_resume_without_rerunning_completed_nodes`
runs a workflow to its approval pause, closes the application context, builds a new one
(a new worker identity), approves, and checks that every node ran exactly once and no
child run was duplicated. The Playwright spec `workflow-approval.spec.ts` does the same
through the UI.

**Run statuses.** `queued -> preparing -> running` when a tick starts;
`running -> waiting_for_approval` when nothing is running and at least one node waits for
a decision; an approval decision moves it back to `queued`. Terminal: `succeeded`,
`failed`, `cancelled`.

**Cancellation.** `POST /runs/{id}/cancel` on a workflow run moves it to `cancelling` (or
straight to `cancelled` if it was queued or waiting). The cancel hook enqueues the run
with high priority, so queued children are cancelled before they start; the engine
cancels running children through `RunService.cancel_run` (their processes are stopped by
the agent executor), cancels pending approvals (audited), cancels running inline nodes
(their process groups are killed), skips queued nodes, and settles the run as
`cancelled`.

**Concurrency.** `settings.max_parallel` bounds how many nodes of one workflow run
execute at once. Cross-run limits are enforced when a worker claims queued work by the
claim guards in `limits/concurrency.py` (global, per adapter, per project, and per
workflow via `Workflow.max_concurrency`, which counts the workflow's runs and their child
runs). The engine does not duplicate those checks.

**Other entry points.** Schedules and webhooks create workflow runs with
`kind="workflow"`, the version id, and the definition in `extra_snapshot["workflow"]`
(`application/services/schedules.workflow_run_fields`); the engine applies input defaults
and types and locks the version on first start, exactly as `WorkflowService.start_run`
does.

## 5. Approvals and the safety policy

Approval requests are first-class rows: type, title, rationale, risk level, requested
action, status (`pending`, `approved`, `rejected`, `expired`, `cancelled`), requester,
reviewer, reviewer note, created, expiration, and resolution time. Decisions go through
`ApprovalService.approve` / `reject` (a conditional update, so two reviewers cannot both
decide), write an audit entry and an `approval_*` event, and call the request's
`resume_handler`. `expire_overdue()` runs from the worker's maintenance loop and the
workflow sweep; an expired approval fails the waiting node with `approval_expired`.

The **inbox** (`/approvals` in the UI, `GET /api/v1/approvals?status=pending`) lists
every request across runs and workflows.

### Policy (`domain/policy.py`)

**Hard-disallowed in V1, even with an approval** (`check_command` returns `deny`; command
nodes fail with `policy_violation`):

| Action | Examples refused |
|---|---|
| Automatic push / force push | `git push`, `git push --force`, `git push origin +main` |
| Automatic merge | `git merge`, `git pull`, `git rebase`, `git cherry-pick`, `gh pr merge` |
| Destructive Git reset | `git reset --hard`, `git clean -f`, `git checkout -- .`, `git stash drop` |
| Deleting branches or foreign worktrees | `git branch -D`, `git push --delete`, `git worktree remove <not AgentTape's>` |
| Host filesystem deletion | `rm`, `rmdir`, `shred`, `find -delete` |
| Displaying secret values | `printenv`, `env` with no command |
| Git options that run a program or write a chosen file | `git fetch --upload-pack=...`, `git grep -O...`, `git log --output=...`, `git -c core.sshCommand=...`, `git --config-env ...` |

Shells (`sh -c ...`, `env -S ...`) and privilege escalation (`sudo`) are refused
outright. Wrappers (`env X=1 ...`, `timeout 5 ...`, `nice -n 5 ...`, `xargs`, `setsid`...)
are parsed with their own option syntax: a deny for the wrapped program always stands,
and the wrapper itself must be allowlisted. `git fetch` needs an approval.

**Template values never become options.** A command node's argv is rendered from
`{{ inputs.* }}` and `{{ nodes.*.outputs.* }}` (which can carry webhook payloads or agent
output). When a substitution supplies the first character of an argument and the result
starts with `-`, the node fails with `policy_violation` before anything runs; for `env`
and `make` a substituted `NAME=value` is refused too. Write the option name literally
(`"--branch={{ inputs.ref }}"`) so the input only supplies its value.

**Approval required by default** (`APPROVAL_REQUIRED_BY_DEFAULT`):

| Approval type | When |
|---|---|
| `commit` | committing Git changes (the Git service's commit approvals use the same type) |
| `elevated_command` | a command marked `elevated`, or an executable outside the allowlist |
| `protected_path` | writing a path that matches the project's protected patterns |
| `budget_increase` | raising or removing an established run budget limit |
| `evaluation_override` | continuing after a failed required evaluation, when the gate permits override at all |

The command allowlist is the project's `command_allowlist` setting when present,
otherwise `DEFAULT_COMMAND_ALLOWLIST` (git, make, pytest, ruff, mypy, npm, pnpm, yarn,
cargo, and read-only shell tools; interpreters are not included). A project's own
`validation_commands` are trusted as **exact** argument arrays (for the allowlist only:
the hard-disallowed list still applies, a denied one cannot be saved, and one that is
denied or needs approval is not run after an attempt). A command node that needs
approval pauses with an `elevated_command` request whose `requested_action` carries a
fingerprint of the exact argv and cwd; approval re-runs the node only for that exact
command. `GET /api/v1/policy` lists the policy and `POST /api/v1/policy/check-command`
checks an argv.

### Service API for other services (`application/services/safety.py`)

`SafetyApprovals(ctx)` creates typed approvals inside the caller's transaction and
verifies them before acting. Every request stores a `fingerprint` of the action, and
`require_approved` checks status, type, and fingerprint, so an approval for one action
can never authorise a different one.

```python
from agenttape.application.services.safety import SafetyApprovals, commit_action
from agenttape.domain.policy import ApprovalType

safety = SafetyApprovals(ctx)
with ctx.session() as session:
    approval = safety.request_commit(
        session, run_id=run_id, repo_path=str(worktree), branch="agenttape/run-1",
        message="Validate names", files=["src/greeter.py"],
        resume_handler="git_commit",       # your handler: register_approval_handler(...)
        expires_in_seconds=86400,
    )

# Before committing (e.g. inside the resume handler):
with ctx.session() as session:
    safety.require_approved(
        session, approval_id, ApprovalType.GIT_COMMIT,
        action=commit_action(str(worktree), "agenttape/run-1", "Validate names", ["src/greeter.py"]),
    )   # raises PolicyViolationError unless approved and identical
```

Also: `request_elevated_command(session, argv=, reason=, ...)` (refuses hard-disallowed
commands), `request_protected_path_write(session, paths=, patterns=, ...)` and
`request_budget_increase(session, run_id=, current=, proposed=, reason=, ...)` (both
return `None` when no approval is needed), `request_evaluation_override(...)`, and the
generic `request(session, type=, title=, rationale=, action=, ...)`.

## 6. Evaluations

**Evaluators** (`evaluations/builtin/`) return `pass`, `fail`, `warning`, `error`, or
`skipped`, with structured evidence, an explanation, a duration, and the evaluator
version.

| Type | Checks | Deterministic |
|---|---|---|
| `exit_code` | the attempt's exit code | yes |
| `command` | an allowlisted test/check command in the run's working directory exits as expected | no |
| `file_exists` / `file_absent` | paths or globs inside the working directory | no |
| `git_diff` | max files, max changed lines, protected paths, from recorded file changes; `skipped` when the run recorded no change data | yes |
| `regex` | a pattern must match / be absent in stdout, stderr, agent messages, or outputs | yes |
| `json_schema` | outputs (or a dotted path, or a JSON artifact) against a JSON Schema (draft 2020-12) | yes |
| `artifact_produced` | artifacts matching a name glob, kind, media type, size | yes |
| `duration` | fails over `max_seconds`, warns over `warn_seconds` | yes |
| `usage_cost` | token and cost limits; `skipped` as unavailable when usage is not reported, and simulated (Demo Agent) usage never counts as real | yes |
| `human_score` | a person's score and notes (`POST /runs/{id}/evaluations/human`); automatic runs report `skipped` ("waiting for a person") | no |
| `agent_judge` | an optional judge backend grades against a rubric; **disabled** (`skipped`, listed unavailable) unless a backend is registered with `register_judge_backend`; none ships in V1 | no |

**Definitions** (`/evaluation-definitions`) name an evaluator and configuration; each
change is a new immutable version, locked when first used. **Suites**
(`/evaluation-suites`) group definitions, optionally pinning versions or overriding
`required`. A required evaluation that fails fails an agent run (`evaluation_failed`) or
an evaluation gate.

On any finished run: `POST /runs/{id}/evaluations/run` runs definitions or a suite now;
`POST /runs/{id}/evaluations/rerun` reruns the run's **deterministic** evaluations
against its recorded data (new rows with `rerun_of_id`; others are reported as skipped);
`GET /evaluations/compare?run_id=A&run_id=B` returns a per-evaluation matrix across 2 to
6 runs with a `differs` flag for the compare view.

## 7. UI

- **Workflows** (`/workflows`): list; detail with graph and list views, versions (locked
  versions are marked), recent runs, and a start form with typed inputs; editor
  (`/workflows/new`, `/workflows/:id/edit`) with an editable React Flow graph (drag to
  connect, select a connection and press Delete to remove it), the node list, a
  per-node form (config, inputs, dependencies and edge conditions, timeout, attempts,
  required, continue on failure, schemas), workflow settings and inputs, and server
  validation before saving a new version.
- **Run detail**: a Workflow tab (DAG with live node statuses, the ordered step list, and
  a node inspector with resolved inputs, outputs, child run, and approval) and an
  Evaluations tab with rerun, run-suite, and human-score actions.
- **Approvals** (`/approvals`): the inbox, pending and decided.
- **Evaluations** (`/workflows/evaluations`): definitions, versions, and suites.

The graph is not the only representation: the ordered node list names every node, its
type, what it waits for, and its status, and is keyboard navigable.

## 8. Limits and known gaps

- Inline command nodes hold a worker slot while the command runs (child agent runs do
  not). A command interrupted by a crash is re-run, because it never completed.
- The command policy inspects argument arrays; it cannot see what an allowlisted build
  tool or an operator-allowlisted interpreter does internally. That is why interpreters
  are not allowlisted by default and elevated commands always need approval.
- No agent-as-judge backend ships in V1; the interface exists and reports "disabled".
