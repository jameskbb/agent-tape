# Deployment

AgentTape is built for one person on one machine: a laptop, a workstation, or a homelab
box. There is no hosted service. This page covers running it natively (recommended), in
a container, and behind a reverse proxy, plus data, backups, upgrades, and PostgreSQL.

## Native (recommended)

Needs Python 3.12+, [uv](https://docs.astral.sh/uv/), Node 22.22+ with pnpm (only to
build the UI), and git.

```bash
make setup      # uv sync, pnpm install, agenttape init (creates ~/.agenttape)
make build      # build the web UI into the Python package
cd backend && uv run agenttape serve
```

`agenttape serve` starts the API, the web UI, the worker, and the scheduler in one
process on `http://127.0.0.1:8787`. It applies database migrations on start
(`--no-migrate` to skip). Split them if you prefer separate processes:

```bash
uv run agenttape serve --no-worker --no-scheduler   # API and UI only
uv run agenttape worker                             # one or more workers
uv run agenttape scheduler                          # standby-safe: one owner via a DB lease
```

To keep it running, use a user-level service manager. A systemd user unit:

```ini
# ~/.config/systemd/user/agenttape.service
[Unit]
Description=AgentTape
After=network.target

[Service]
WorkingDirectory=%h/src/agent-tape/backend
Environment=AGENTTAPE_ALLOWED_ROOTS=%h/src
ExecStart=%h/.local/bin/uv run --frozen agenttape serve
Restart=on-failure

[Install]
WantedBy=default.target
```

`systemctl --user daemon-reload && systemctl --user enable --now agenttape`. Stopping the
service interrupts running attempts; they are recorded as `interrupted` and retried if
their retry policy allows.

## Windows (through WSL)

AgentTape needs POSIX process groups, so on Windows it runs inside WSL2. Clone and set up
the repository inside WSL (the default path is `~/src/agent-tape`), then start it from
Windows with the launcher in `scripts/windows/`. It runs `agenttape serve` in WSL and
opens the UI in your Windows browser. WSL forwards `127.0.0.1`, so the server stays on
loopback and needs no API token.

```powershell
.\scripts\windows\AgentTape.ps1 -Setup            # first run: make setup, make build, agenttape init
.\scripts\windows\AgentTape.ps1                   # start if needed, open the browser
.\scripts\windows\AgentTape.ps1 -Status           # is it up?
.\scripts\windows\AgentTape.ps1 -Stop             # graceful stop
.\scripts\windows\AgentTape.ps1 -InstallShortcut  # add "AgentTape" to the Start menu
```

`AgentTape.cmd` does the same when you double-click it. The launcher can run from a
Windows clone or from the WSL checkout (`\\wsl.localhost\<distro>\...`), but it always
serves the WSL checkout. The server runs in a minimized window titled "AgentTape server",
and closing that window also stops it. Override the defaults with `-Distro`, `-RepoPath`
and `-Port`, or with the `AGENTTAPE_WSL_DISTRO`, `AGENTTAPE_WSL_REPO` and
`AGENTTAPE_PORT` environment variables.

## Configuration

Every setting is an environment variable; `.env.example` lists them with safe defaults.
The ones that matter for deployment:

| Variable | Default | Notes |
|---|---|---|
| `AGENTTAPE_DATA_DIR` | `~/.agenttape` | Database, artifacts, raw logs, owned worktrees. |
| `DATABASE_URL` | SQLite in the data dir | PostgreSQL: see below. |
| `AGENTTAPE_HOST` / `AGENTTAPE_PORT` | `127.0.0.1` / `8787` | Anything other than loopback requires a token. |
| `AGENTTAPE_API_TOKEN_HASH` | none | `sha256:<hex>`; or `agenttape token create` writes it to the data dir. |
| `AGENTTAPE_REQUIRE_TOKEN` | `false` | Require the token on loopback too. |
| `AGENTTAPE_ALLOWED_HOSTS` | (none) | Comma-separated extra `Host` names accepted without a token (loopback names always are). Other names get `421`, which blocks DNS rebinding. Not needed once a token is required. |
| `AGENTTAPE_ALLOWED_ROOTS` | none | Directories projects may live in (`:` or `,` separated). Also editable in Settings. |
| `AGENTTAPE_REDACT_ENV` | none | Names of variables whose values are always redacted. |
| `AGENTTAPE_WORKER_CONCURRENCY` | `2` | Parallel runs per worker process. |
| `AGENTTAPE_OBSERVED_IDLE_TIMEOUT_SECONDS` | `3600` | Close an observed Claude Code session's run after this many seconds without hook activity. See [integrations/claude-code.md](integrations/claude-code.md). |
| `AGENTTAPE_HERDR_SESSION` | none | Herdr session name (Herdr's default session when empty). See [HERDR.md](HERDR.md). |
| `AGENTTAPE_HERDR_SOCKET` | none | Explicit Herdr API socket; overrides the session. |
| `AGENTTAPE_HERDR_EXECUTABLE` | `herdr` | Herdr CLI, when it is not on `PATH`. |
| `AGENTTAPE_HERDR_MACHINE` | `local` | Machine label recorded on runs (a saved `herdr machine` name for remote servers). |

Herdr is optional. When it is installed, the AgentTape service must run as the same user
as the Herdr server so it can reach the socket; in a container, Herdr is only reachable if
you mount its socket, which is not recommended. File evidence uses the OS file watcher
(inotify on Linux); raise `fs.inotify.max_user_watches` for very large repositories.

## Exposing AgentTape beyond this machine

Create a token and keep the printed value somewhere safe; only its hash is stored:

```bash
cd backend
uv run agenttape token create          # prints the token once, stores sha256 in the data dir
uv run agenttape serve --host 0.0.0.0
```

`serve` refuses to bind beyond loopback without a token. Clients send
`Authorization: Bearer <token>`; in the web UI, paste it under Settings, "API token for
this tab" (kept in sessionStorage, so it is cleared when the tab closes). Put AgentTape behind a TLS-terminating reverse proxy (Caddy, nginx,
Traefik) so the token never crosses the network in clear text. The proxy must not buffer
`/api/v1/runs/*/events/stream` (Server-Sent Events); AgentTape already sends
`X-Accel-Buffering: no` for nginx. Webhook senders need to reach
`/api/v1/webhooks/<trigger id>` only; they authenticate with their trigger's token.

## Container

The repository ships a multi-stage `Dockerfile` (builds the UI, installs the package with
uv, runs `agenttape serve` as the non-root user `agenttape`, uid 10001 unless you build
with other ids) and a `docker-compose.yml` example.

```bash
TOKEN=$(python3 -c 'import secrets; print("atk_" + secrets.token_urlsafe(32))')
echo "$TOKEN"   # keep it; the UI and API clients need it
export AGENTTAPE_API_TOKEN_HASH=$(echo -n "$TOKEN" | uv run --project backend agenttape token hash)
export AGENTTAPE_UID=$(id -u) AGENTTAPE_GID=$(id -g)   # Linux: see the note on ./repos below
mkdir -p repos  # repositories agents may work in (mounted at /repos)
docker compose up --build
```

Then open `http://127.0.0.1:8787` and paste the token under Settings, "API token for this
tab"; until then the UI shows "A valid API token is required". To load the demo data, run
`docker compose exec agenttape agenttape seed`.

- The container listens on `0.0.0.0` inside its network namespace, so the token is
  mandatory; Compose publishes the port on host loopback (`127.0.0.1:8787`) by default.
- Data lives in the `agenttape-data` volume at `/data`.
- Repositories under `./repos` are mounted at `/repos`, which is the allowlisted root.
  On Linux they keep their host owner, so the container user must have the same ids:
  build with `AGENTTAPE_UID` and `AGENTTAPE_GID` as above. With the default uid 10001,
  Git refuses a repository you own ("dubious ownership"), AgentTape reports it as not a
  Git repository, and agents cannot write to it. The ids are fixed at build time and own
  the data volume, so changing them on an existing volume needs a `chown` of `/data`.
- Worktrees live in the data volume, so the `git worktree` entries AgentTape adds to a
  mounted repository name paths that exist only inside the container.
- Agent CLIs (Claude Code, Codex) are not installed in the image; the Demo Agent and the
  generic command adapter work out of the box. Build a derived image to add others.
- The image has no PostgreSQL driver, so it runs on SQLite only.

**Validation status.** Verified by hand with Docker 29.9.0 and Compose 5.6.0 on Linux
(WSL2): the image builds, Compose comes up healthy, `/api/v1/health` and `/api/v1/ready`
answer without a token, other API routes and the UI's data need it, the UI loads and works
in a browser after pasting the token, a Demo Agent run succeeds, a generic-command run in
an isolated worktree of a repository under `./repos` succeeds, and data survives `docker
compose restart` and `docker compose down` followed by `up` (not `down -v`, which deletes
the volume). No CI runs this; `make check` runs static checks and `docker build` only
(reported `SKIPPED-EXTERNAL` without Docker).

## Data, backups, and retention

Everything AgentTape records is in the data directory:

| Path | Contents |
|---|---|
| `agenttape.db` (+ `-wal`, `-shm`) | SQLite database |
| `artifacts/` | artifact files (content-hashed, written atomically) |
| `logs/<run>/` | full redacted raw output per attempt |
| `workspaces/` | AgentTape-owned worktrees and demo clones |
| `fixtures/demo-repo/` | the Demo Agent's repository |
| `api_token.sha256` | local API token hash, if created |

Back up with the service stopped, or use SQLite's online backup:
`sqlite3 ~/.agenttape/agenttape.db ".backup '/backups/agenttape.db'"`, then copy
`artifacts/` and `logs/`. Retention (Settings, Data retention) deletes finished runs
older than a threshold after a dry-run preview and never touches your repositories.

## Upgrades

Pull the new version, then `make setup && make build` and restart. `serve` applies
migrations on start; `uv run agenttape migrate` does it explicitly. Take a backup first.
When the worker starts it also gives findings recorded before the findings table existed
their rows (from the runs' own recorded finding events, idempotently), so older runs keep
showing their findings in the Findings tab and inbox.
Migrations are tested for upgrade, downgrade, and re-upgrade on SQLite in `make check`;
for PostgreSQL see below.

## PostgreSQL

SQLite is the default and the only database `make check` runs against. PostgreSQL 17 was
verified by hand, not by CI: the Python test suite passes on it (2134 passed, 1
SQLite-only test skipped), Alembic migrations upgrade, downgrade, and re-upgrade with no
schema drift from the models, and `serve` runs the demo and keeps its data across a
restart. The native install needs the driver added; the container image does not include
it.

```bash
cd backend && uv add 'psycopg[binary]'
export DATABASE_URL=postgresql+psycopg://agenttape:change-me@localhost:5432/agenttape
uv run agenttape migrate && uv run agenttape serve
```

`uv add` changes `pyproject.toml` and `uv.lock` in your checkout.
[DEVELOPMENT.md](DEVELOPMENT.md#postgresql) describes how to run the test suite against
PostgreSQL.
