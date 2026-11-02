# ADR 0003: Database-backed queue with leases

- Status: Accepted
- Date: 2026-09-23

## Context

Runs must survive restarts, be claimed by exactly one worker, respect priority, and be
recoverable when a worker dies. Redis and Kafka are out of scope.

## Decision

`queue_items` rows (status `queued | leased | done | cancelled`) with `priority`,
`enqueued_at`, `available_at` (for backoff), `lease_owner`, `lease_expires_at`, and a
unique `dedupe_key` (`run:<id>:<generation>`) so an enqueue can never duplicate.

- **Claim**: read a small batch of candidate ids ordered by priority desc, then
  enqueued_at asc; for each, `UPDATE ... SET status='leased', lease_owner=:me ...
  WHERE id=:id AND status='queued'` in its own transaction and accept the first row whose
  `rowcount` is 1. Atomic on SQLite (single writer) and PostgreSQL (row lock and WHERE
  re-check), so two workers never lease the same item (tested with threads and with
  separate engines).
- **Leases**: the worker renews the queue lease and the attempt's `lease_expires_at` and
  `heartbeat_at` every `lease_seconds / 3`. Losing the lease cancels the attempt.
- **Reconciliation** (on worker start and every heartbeat interval): attempts in
  preparing/running/cancelling with an expired lease become `failed/interrupted` (or
  `cancelled` if a cancel was requested); the run is re-queued if its retry policy allows,
  otherwise failed. A persisted PID is informational only and never signalled. Expired
  leases on items whose run is still `queued` are reopened.
- **Named leases** (`scheduler_leases`) give singleton loops such as the scheduler one
  active owner, using the same conditional-update technique.
- The `Queue` interface (`application/ports.py`) allows another backend later.

## Consequences

- Throughput is bounded by database writes, which is ample for local agent work.
- Queue position is computed on read from priority and FIFO order.
