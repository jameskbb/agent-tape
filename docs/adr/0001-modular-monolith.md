# ADR 0001: Modular monolith with separately runnable processes

- Status: Accepted
- Date: 2026-09-23

## Context

AgentTape runs on one developer's laptop or homelab, but it has three kinds of work with
different lifetimes: an HTTP API and web UI, workers that supervise long-running agent
processes, and a scheduler that must have exactly one active owner. The design rules out
microservices, Redis, and Kafka, and requires that the UI, CLI, API, scheduler, and
webhooks share one execution engine.

## Decision

One Python package (`agenttape`) organised by boundary:

| Package | Responsibility |
|---|---|
| `domain/` | Pure types and rules: statuses and transition tables, failure categories, event envelope, prompt variables, retry policy. No I/O. |
| `application/` | Services (use cases) and ports. `RunService` is the only way to create, cancel, retry, or read runs. |
| `adapters/` | Agent adapter protocol, subprocess helper, registry, built-in adapters. |
| `infrastructure/` | SQLAlchemy models and migrations, DB queue, leases, artifact store, clock. |
| `api/` | FastAPI transport only; routers call services. |
| `worker/`, `scheduler/`, `runtime/` | Long-running loops and the supervisor that hosts them. |
| `security/`, `git/`, `evaluations/`, `artifacts/`, `cli/`, `seeds/` | As named. |

Processes: `agenttape serve` runs the API and every registered background service (the
worker and the scheduler) as asyncio tasks under one supervisor.
`agenttape worker` and `agenttape scheduler` run a single service standalone, so
the same code scales to several processes sharing one database. Every entry point builds
the same `AppContext` through `agenttape.bootstrap.build_context`.

Extension points are registries of files (routers, CLI commands, adapters, evaluators,
run executors, background services, seeds, frontend features, run-detail tabs) so
parallel work adds files instead of editing a shared list.

## Consequences

- One deployable, one migration history, one test suite; no network hops between parts.
- Boundaries are enforced by convention and review, not by process isolation.
- Moving the worker to another host later needs only a PostgreSQL `DATABASE_URL`; the
  queue and lease design already assumes multiple processes.
