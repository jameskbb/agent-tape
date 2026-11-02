# Troubleshooting

Start with `uv run agenttape doctor` (from `backend/`). It checks Python, the data
directory, the database and migrations, the scheduler lease, live workers, the queue,
git, every adapter, the built UI, the bind address, and the allowed project roots,
without changing anything. `--json` prints the same as machine-readable output.

## Runs

**A run stays `queued`.** No worker is running, or a concurrency limit is holding it.
`doctor` shows `workers: no live worker` in the first case: start `agenttape serve` (it
includes a worker) or `agenttape worker`. `agenttape run --follow` and `agenttape demo`
execute in-process when no worker is live. For limits, the run's Limits tab and
Settings, Limits show which scope is full.

**A run failed with `interrupted`.** The worker stopped heartbeating (it was killed, the
machine slept, or the service restarted) and reconciliation settled the attempt. The run
is retried automatically if its profile allows another attempt; otherwise use Retry.
AgentTape never assumes an old PID still belongs to the attempt.

**`timed_out` with `idle_timeout`.** The agent printed nothing for the idle limit. Raise
`idle_timeout_seconds` on the profile or project for tools that think quietly.

**`config_error` about a secret.** A secret reference could not be resolved: the
environment variable it points at is not set in the process that runs the worker.
Secret values are read when an attempt starts, not when the run is created.

**`adapter_unavailable` or a missing executable.** `doctor` shows each adapter's status.
Claude Code and Codex are discovered on `PATH` (or `AGENTTAPE_CLAUDE_EXECUTABLE` /
`AGENTTAPE_CODEX_EXECUTABLE`); discovery never checks credentials, so an unauthenticated
CLI fails on its first run with a clear message. The generic command adapter only runs
executables on the allowlist (Settings or `agenttape executables`).

**Output looks cut off.** Each attempt stores up to 2 MB per stream as timeline events;
the rest is in the raw log (Attempts tab, stdout/stderr download), which keeps up to
50 MB per stream. A warning event marks where the stored events stop.

**I see `[REDACTED:...]` in my output.** Redaction removed something shaped like a
credential before it was stored. This is deliberate and may occasionally catch a
harmless value; the raw value is never recoverable from AgentTape.

## Claude Code capture

Problems recording findings or hooks from Claude Code sessions (a finding the agent was
told was not recorded, no run for an interactive session, citations showing Read not
observed) are covered in [claude-code.md, Troubleshooting capture](integrations/claude-code.md#troubleshooting-capture).

## Projects and Git

**"Path is outside the allowlisted project roots".** Add the parent directory to
`AGENTTAPE_ALLOWED_ROOTS` or to the roots in Settings. Symlinks are resolved first, so a
link inside a root that points outside it is refused.

**"git ... is not allowed".** The guarded Git runner refuses push, merge, reset, and other
destructive operations on purpose (docs/SECURITY.md). AgentTape only commits locally, in
its own worktree, after an approval.

**A worktree will not clean up.** Cleanup refuses when the worktree has uncommitted work.
Commit it (with approval) or copy what you need, then clean up from the Changes tab or
`agenttape git cleanup <run>`.

## Server and UI

**`serve` says "Refusing to bind".** You asked for an address beyond loopback without an
API token. Run `agenttape token create` first (or set `AGENTTAPE_API_TOKEN_HASH`).

**The browser shows "AgentTape API is running" instead of the app.** The UI has not been
built into the package: run `make build` and restart `serve`, or use `make dev` for the
Vite dev server on port 5173.

**API calls return 401 or 503.** 401: the token is missing or wrong (paste it under
Settings in the UI, or send `Authorization: Bearer <token>`). 503 `token_not_configured`:
token auth is required but no hash is configured on the server.

**The timeline stops updating.** The live stream reconnects on its own with the last
sequence it saw. Behind a proxy, make sure Server-Sent Events are not buffered
(docs/DEPLOYMENT.md).

**Port 8787 is in use.** `agenttape serve --port 8788` or set `AGENTTAPE_PORT`.

## Scheduler

**A schedule did not fire.** Check it is enabled (seeded schedules start paused), look at
its History for `skipped` or `missed` occurrences (misfire and concurrency policies), and
check `doctor` for the scheduler lease. Only one scheduler owns the lease; others wait as
standbys.

## Database

**"database is locked".** Another process held a write lock longer than the 10 second
busy timeout, usually a long manual `sqlite3` session. Close it. For many concurrent
workers, use PostgreSQL.

**`doctor` says migrations are behind.** Run `uv run agenttape migrate` (take a backup of
the data directory first).

## Development

**Playwright screenshots hang under WSL2.** The Playwright config launches Chromium with
SwiftShader flags for this reason; custom scripts need the same `--use-gl=swiftshader
--disable-software-rasterizer --disable-gpu` arguments.

**`make check` reports `SKIPPED-EXTERNAL`.** A gate could not run in this environment
(for example Docker is not installed). The summary says why; it is never counted as a
pass, and `AGENTTAPE_STRICT_EXTERNAL=1` turns it into a failure.
