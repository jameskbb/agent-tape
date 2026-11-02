# Scheduling, budgets, and limits

This covers the scheduler (schedules, time zones, DST, misfires, overlap), budgets, and
concurrency limits. Webhook triggers are in [TRIGGERS.md](TRIGGERS.md).

## Schedules

A schedule runs a saved prompt with an agent profile (or, once the workflow executor is
installed, a workflow) on one of three timings, always with an explicit IANA time zone:

| Type | Fields | Meaning |
|---|---|---|
| `one_time` | `run_at` | One run at that instant. |
| `interval` | `interval_seconds` (60 s to 366 days), optional `start_at` | `anchor + k * interval`, where the anchor is `start_at` or the schedule's creation time. Elapsed time: DST never shifts it, so a 24-hour interval moves by an hour on the wall clock when clocks change. |
| `cron` | `cron_expression` (5 fields) | Matches local wall-clock time in the schedule's zone. |

Optional `start_at` and `end_at` bound every type. Times sent without an offset are local
wall time in the schedule's zone (`"2026-03-10T09:00"` with `Europe/Berlin` is 08:00 UTC);
times with an offset are converted. Everything is stored in UTC, and the API also returns
`*_local` fields rendered in the schedule's zone.

### Daylight saving time (cron)

* **Spring forward.** A wall time that does not exist (02:30 in New York on 8 March 2026)
  fires at the first instant after the gap (03:00 EDT). Several missing wall times collapse
  into that single instant, so nothing fires twice.
* **Fall back.** A wall time that happens twice (01:30 in New York on 1 November 2026)
  fires once, at its first occurrence.

`POST /api/v1/schedules/preview` returns the next N run times with a `note` on any
occurrence that a DST change affects. The UI's preview updates as you type. The unit tests
in `backend/tests/unit/test_schedule_calc.py` cover both transitions.

### Missed runs (misfire policy)

An occurrence is late when its due time passed more than 60 seconds before the scheduler
saw it (the scheduler was stopped or the machine slept).

* `run_once` (default): if any occurrences were missed, one run starts for the most recent
  one; the others are recorded as `missed`.
* `skip`: late occurrences are recorded as `missed` and nothing runs until the next time.

Pausing is different: resuming computes the next run from now, so a paused period is never
back-filled. Run now works while paused.

### When the previous run is still going (concurrency policy)

A schedule's run is "active" while it is queued, preparing, running, cancelling, or waiting
for approval.

* `queue` (default): the new occurrence waits (status `queued`) and starts when the active
  run finishes. At most `max_queued` wait; more are `skipped`. `max_queued: 0` means never
  wait.
* `skip`: the occurrence is recorded as `skipped`.
* `cancel_and_replace`: the active run is cancelled (its occurrence becomes `replaced`) and
  the new run starts.

Run now applies the same policy. `POST /schedules/{id}/run-now` with `{"force": true}`
(or `agenttape schedules run-now --force`) ignores it.

### History

Every due time is a row in `schedule_occurrences` with a status: `created`, `queued`,
`skipped`, `missed`, `replaced`, `failed` (the run could not be created, for example because
the prompt was archived; the reason is recorded), or briefly `pending`. The schedule page
and `GET /schedules/{id}/history` show them with the run each one created.

## The scheduler

`agenttape serve` runs the scheduler in-process; `agenttape scheduler` runs it on its own.
Each pass (every 2 seconds):

1. Acquire or renew the database lease named `scheduler` (TTL 30 s). Without it the
   instance is a standby and does nothing else, so two schedulers never both evaluate
   schedules. If the owner dies, a standby takes over when the lease expires.
2. Finish `pending` occurrences left by a crash between inserting an occurrence and
   creating its run.
3. For each enabled schedule with `next_run_at <= now`: collect the due times, advance
   `next_run_at` with a compare-and-set (a second scheduler that raced it stops here),
   apply the misfire policy, and insert occurrence rows. `UNIQUE (schedule_id,
   occurrence_at)` backs up the lease: an occurrence can only be inserted once.
4. Apply the concurrency policy and create the run through `RunService.create_run` with
   `source=schedule` and the idempotency key `schedule:<schedule id>:<occurrence time>`,
   so even a retried creation returns the same run. The run gets a `scheduler` event
   naming the schedule and occurrence.
5. Start queued occurrences whose schedule no longer has an active run.

Nothing lives only in memory, so stopping and restarting at any point resumes correctly.
`backend/tests/integration/test_scheduler.py` covers two racing scheduler instances (with
and without the lease), restarts with missed occurrences, and crash recovery.
`GET /api/v1/scheduler/status` (and the banner on the Schedules page) reports whether a
scheduler holds the lease.

CLI: `agenttape schedules list | show | run-now | pause | resume | preview`.

## Budgets

A budget limits what one run may use. It is merged from, lowest precedence first: the
global default budget (Settings > Limits), the project's `settings.budget`, the agent
profile's `config.budget`, and the run's `budget` (`RunCreate.budget`).

| Key | Unit | Measured from |
|---|---|---|
| `max_total_tokens` | tokens | Real reported usage (`input_tokens + output_tokens`, or `total_tokens`) |
| `max_cost_usd` | USD | Real reported cost (`usd`, `total_usd`, or `amount` with `currency: USD`) |
| `max_changed_files` | files | `file_changes` rows of the attempt |
| `max_diff_lines` | lines | Insertions plus deletions of those rows |
| `max_artifact_bytes` | bytes | Total size of the run's `artifacts` rows |
| `warn_ratio` | ratio | When to warn (default 0.8) |

While an attempt runs, a monitor checks every second. When a budget crosses the warn ratio
it records one `warning` event (`payload.kind = "budget_warning"`). When a budget is passed
it records an `error` event (`payload.kind = "budget_exceeded"` with every check) and stops
the attempt. The run fails with `failure_category: budget_exceeded`, which is not retried.
After the attempt a final check runs on the adapter's reported totals (after other
after-attempt hooks such as diff capture) and records `outputs.budget`.

Usage events are treated as increments during the attempt; the adapter's final result is
authoritative afterwards.

**No invented precision.** Usage marked `simulated` (the Demo Agent) and cost that was not
reported never count: those checks show `not_applicable` or `unknown`, never zero.

Time and attempts use the existing run options: the wall-clock limit (`timeout_seconds`)
gets a warning at the warn ratio, the idle limit is enforced by the worker, and the final
attempt of a retrying run gets a notice. The run detail's **Limits** tab
(`GET /runs/{id}/limits`) shows the effective budget, each check, the timeouts, the retry
policy (categories and waits), and the concurrency scopes that apply.

## Concurrency limits

| Scope | Where it is set |
|---|---|
| All runs | Settings > Limits, `max_concurrent_runs` |
| Per adapter | Settings > Limits, `per_adapter` (`{"claude-code": 1}`) |
| Per project | The project's `settings.max_concurrent_runs` |
| Per workflow | The workflow's `max_concurrency` |

A run holds a slot while its queue item is leased (for its whole attempt). Runs waiting for
approval or for a retry back-off hold no slot. The limit is checked by a queue claim guard
after the claim commits: concurrent claimers always see each other, so they can never both
pass a limit. A run that must wait goes back to the queue for about a second and gets one
notice event saying what it is waiting for. Each worker also runs at most
`AGENTTAPE_WORKER_CONCURRENCY` attempts.

`GET|PUT /api/v1/settings/limits` read and change the global settings (audited).
