# AgentTape Agent Operating Rules

`CLAUDE.md` and `AGENTS.md` are the same document under two names so Claude
Code and Codex follow identical rules. Change both together; `scripts/check.sh`
fails if they differ. Human contributors should also read
[`CONTRIBUTING.md`](CONTRIBUTING.md).

## 1. What this repository is

AgentTape lets you trust but verify AI coding agents: agents cite evidence for
their findings, and AgentTape checks those citations against its own recording
of the run. Underneath, it is a local-first control plane, flight recorder,
scheduler, and orchestration platform for AI coding agents.
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) describes the boundaries,
[`docs/EXTENDING.md`](docs/EXTENDING.md) says where a change belongs, and
[`docs/FINDINGS.md`](docs/FINDINGS.md) explains verified findings.

## 2. Layout

| Path | What lives there |
|---|---|
| `backend/` | Python 3.12+ package `agenttape` (FastAPI API, worker, scheduler, CLI), managed with `uv` |
| `frontend/` | React + Vite + TypeScript web UI, managed with `pnpm` |
| `docs/` | Architecture, security, API, adapter, and operations docs; ADRs in `docs/adr/` |
| `scripts/` | Repository scripts, including `check.sh` (the full quality gate) |
| `e2e/` | Playwright end-to-end tests |

## 3. Install, run, test

```bash
make setup    # install backend and frontend dependencies
make dev      # run API + worker + scheduler + Vite dev server
make check    # full quality gate: lint, types, unit, build, e2e, migrations, secret scan
```

`make check` runs `scripts/check.sh`. It must pass before anything lands.

## 4. Branch model and landing

- Single branch `main`, no prod branch. Contributions arrive as pull requests
  against `main`; CI runs `scripts/check.sh`. Never force-push.
- Commits are atomic with an imperative subject and a short body saying why.
  Commits carry no AI attribution trailer.

## 5. Never

- Never commit secrets, `.env` files, runtime databases, logs, build output,
  captured repository contents, or `.planning/` (per-machine GSD state).
- Never pass user-provided command text through a shell (`shell=True`). Store
  executables and argument arrays separately.
- Never fake metrics: token counts, cost, and judge results show "unavailable"
  when an adapter does not report them.
- Never add automatic `git push`, merge, force-reset, or branch deletion to the
  product.
- Never capture or display hidden model reasoning; record only adapter-provided
  observable events and explicit summaries.

## 6. Conventions

- Backend is organized by domain and application boundaries: `domain/`,
  `application/`, `adapters/`, `infrastructure/`, `api/`, `worker/`,
  `scheduler/`, `security/`, `git/`, `evaluations/`, `artifacts/`.
- Extension points are registries of files (one module per adapter, evaluator,
  node type, router) so parallel work does not collide on one shared file.
- UI, CLI, API, scheduler, and webhooks share one application service layer
  and one orchestration engine. Do not add a second code path.
- A change to the interface rewrites any README sentence describing a screen
  that was redesigned, renamed, or removed, and needs fresh README images in
  `docs/images/` before it lands (the maintainer regenerates them; see
  `CONTRIBUTING.md`). Numbers in the README that a test or command prints are
  re-checked the same way.
- **No em dashes.** Never write an em dash in this repository: docs, READMEs,
  comments, UI text, commit messages. Reword with a colon, semicolon, comma,
  parentheses or a new sentence; do not substitute a hyphen or en dash.
  `scripts/check.sh` fails if a tracked file contains one.
