# Development

## Prerequisites

- Python 3.12 or newer and [uv](https://docs.astral.sh/uv/) (0.12 used here)
- Node 22.22 or newer and pnpm (12 used here)
- git (the Demo Agent and Git snapshots use it)
- Linux, macOS, or WSL2 (process-group handling is POSIX)

## Setup and everyday commands

```bash
make setup       # uv sync, pnpm install, agenttape init (creates ~/.agenttape)
make dev         # API + worker on :8787 and Vite on :5173 (proxying /api)
make check       # the full quality gate
```

Other targets: `make test`, `make lint`, `make typecheck`, `make format`, `make build`
(production UI into `backend/src/agenttape/web_dist`), `make migrate`, `make seed`,
`make reset-demo`, `make e2e`, `make openapi`. `make help` lists
them all.

Use a throwaway data directory while hacking:

```bash
export AGENTTAPE_DATA_DIR=$(mktemp -d)
cd backend && uv run agenttape demo          # seed and run the Demo Agent in the terminal
uv run agenttape serve                        # http://127.0.0.1:8787
```

`AGENTTAPE_DEMO_STEP_DELAY_MS=0` makes the Demo Agent run instantly.

## Repository layout

| Path | What lives there |
|---|---|
| `backend/` | Python 3.12+ package `agenttape` (FastAPI API, worker, scheduler, CLI), managed with `uv` |
| `frontend/` | React + Vite + TypeScript web UI, managed with `pnpm` |
| `docs/` | Architecture, security, API, adapter, and operations docs; ADRs in `docs/adr/` |
| `scripts/` | Repository scripts, including `check.sh` (the full quality gate) |
| `e2e/` | Playwright end-to-end tests |

The backend is organized by domain and application boundaries: `domain/`, `application/`,
`adapters/`, `infrastructure/`, `api/`, `worker/`, `scheduler/`, `security/`, `git/`,
`evaluations/`, `artifacts/`. See the boundaries in
[ARCHITECTURE.md](ARCHITECTURE.md). How to add routers, commands, adapters, evaluators,
executors, services, migrations, UI features, and run-detail tabs is in
[EXTENDING.md](EXTENDING.md).

## Backend

```bash
cd backend
uv run pytest -q                       # unit + integration (temp SQLite per test)
uv run pytest tests/integration/test_worker.py -k approval
uv run ruff format . && uv run ruff check --fix .
uv run mypy                            # strict on src/
uv run agenttape --help
```

Migrations: `uv run agenttape migrate` applies all heads. To author a revision, bring a
scratch database to head (`uv run alembic -c alembic.ini upgrade heads`), change the
models, then `uv run alembic -c alembic.ini revision --autogenerate -m "..." --rev-id
<id>` and review the file. Naming and branching rules are in
EXTENDING.md. `scripts/migration_smoke.py` must stay green.

## Frontend

```bash
cd frontend
pnpm dev            # needs the API on :8787 (or AGENTTAPE_API_URL)
pnpm exec vitest    # watch mode
pnpm lint && pnpm format:check && pnpm typecheck
pnpm build
```

The API client is hand-written in `src/api/` (types mirror
`backend/src/agenttape/application/schemas/`); `agenttape openapi` prints the schema to
check against.

A UI change does not touch the images in `docs/images/`; the maintainer updates them.

## End-to-end tests

`make e2e` builds the UI, installs Playwright's Chromium if needed, and runs
`e2e/tests/` against `scripts/e2e-server.sh`, which starts AgentTape on port 8799 (set
`E2E_PORT` to change it) with a fresh temporary data dir and the seeded demo story.

The specs cover the main user journeys (onboarding and a Demo Agent run with a live
timeline, prompt creation and versioning, schedules and run-now, a workflow pausing at
approval and resuming, replay, fork, and compare, Git diff and evaluation views), plus:

- `a11y.spec.ts`: axe-core WCAG 2.1 AA scans (serious and critical findings fail) of 31
  routes, covering nav pages, list and detail pages, creation forms, the workflow editor,
  run detail tabs, compare, and settings sub-pages. Each route is scanned in light and dark
  themes at 1280px and 768px. The spec also checks that no page scrolls sideways at 768px,
  covers keyboard navigation, and checks that the tablet navigation drawer traps focus,
  closes on Escape, and returns focus to the menu button.
- `live-sse.spec.ts`: proves the run timeline updates over Server-Sent Events. After the
  first page loads, REST event requests return nothing, and the run must still reach
  "Run succeeded" with every event shown.
- `workflow-lineage.spec.ts` and `workflow-editor.spec.ts`: workflow replay and compare,
  the explanation shown when a workflow cannot be forked, and editing after creation
  (rename, archive, the unsaved-changes guard, and confirmation before removing a node).

Headless Chromium is launched with SwiftShader flags (`--use-gl=swiftshader
--disable-software-rasterizer --disable-gpu`); without them screenshots hang under WSL2.
Chromium needs its system libraries; on a bare Linux image run
`pnpm --dir e2e exec playwright install --with-deps chromium` (needs root). When the
browser cannot be installed, `make check` reports the e2e gate as `SKIPPED-EXTERNAL` and
is listed with its reason and never counted as a pass (`AGENTTAPE_STRICT_EXTERNAL=1` makes
any skip fail the run).

## PostgreSQL

The suite runs on SQLite by default. It can run against PostgreSQL: set
`AGENTTAPE_TEST_DATABASE_URL` and every test gets that database, reset to an empty schema
(migrations downgraded to base, then upgraded) before it starts. Tests run serially.
Run by hand on PostgreSQL 17: 2134 passed, 1 skipped (a SQLite-only lock test). No CI
workflow runs it yet; this is the recipe:

```yaml
# .github/workflows/postgres.yml (example)
services:
  postgres:
    image: postgres:17
    env: { POSTGRES_USER: agenttape, POSTGRES_PASSWORD: agenttape, POSTGRES_DB: agenttape_test }
    ports: ["5432:5432"]
    options: --health-cmd pg_isready --health-interval 5s --health-retries 10
steps:
  - uses: actions/checkout@v4
  - uses: astral-sh/setup-uv@v6
  - run: cd backend && uv sync --frozen && uv pip install 'psycopg[binary]'
  - run: cd backend && uv run pytest -q
    env:
      AGENTTAPE_TEST_DATABASE_URL: postgresql+psycopg://agenttape:agenttape@localhost:5432/agenttape_test
```

Locally with Docker: `docker run --rm -e POSTGRES_USER=agenttape -e
POSTGRES_PASSWORD=agenttape -e POSTGRES_DB=agenttape_test -p 5432:5432 postgres:17`, then
the same `uv pip install` and `AGENTTAPE_TEST_DATABASE_URL`. Tests that
build their own SQLite URL (migration and pragma tests) keep using SQLite.

## Quality gate

`scripts/check.sh` runs, in order: the no-em-dash check, `uv sync --frozen`,
ruff format check, ruff lint, mypy, pytest, the migration smoke test, `pnpm install
--frozen-lockfile`, eslint, prettier check, tsc for the frontend and for e2e, vitest, the
production build, the secret scan (`scripts/secret_scan.py`, or gitleaks when installed),
the static container checks (`scripts/dockerfile_check.py`), `docker build` (SKIPPED-EXTERNAL without Docker),
and Playwright including the accessibility specs.
