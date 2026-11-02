# ADR 0004: Vendor-neutral event envelope, sequenced persistence, SSE streaming

- Status: Accepted
- Date: 2026-09-23

## Context

Different agents emit very different output. The flight recorder needs one schema that
stores what happened in order, can be filtered and replayed, streams live to browsers,
and never stores secrets or unbounded blobs.

## Decision

- One envelope (documented in `docs/EVENT-SCHEMA.md`): id, run/attempt/step/node/parent
  ids, per-run `sequence`, `ts`, `monotonic_ms`, `type`, `source`, `severity`, `summary`,
  `payload`, `redaction`, optional `usage` and `cost`, `schema_version`.
- Sequence numbers are allocated atomically with `UPDATE runs SET event_seq = event_seq
  + n` inside the inserting transaction; `UNIQUE (run_id, sequence)` backs it up.
- `EventRecorder` is the only writer. It redacts summaries and payloads, caps text fields
  and payload size, and records redaction metadata.
- The worker's sink batches stdout/stderr lines per stream (8 KB / 200 lines / 250 ms),
  keeps a per-attempt stored-output budget (2 MB per stream) and then records one warning.
  Full redacted output always goes to size-capped raw log files in the data dir.
- Live delivery: `GET /api/v1/runs/{id}/events/stream` (Server-Sent Events). Each message
  has `id: <sequence>`; clients resume with `Last-Event-ID` or `?after_sequence=`. The
  stream polls the database (woken early by an in-process notifier) and ends with an
  `end` event when the run is terminal. Oversized messages are replaced by a stub; the
  full event stays available from the paginated `GET /runs/{id}/events`.
- The browser client uses `fetch` streaming rather than `EventSource` so it can send the
  API token header.

## Consequences

- The database never holds unbounded output; raw logs are the complete record.
- Polling adds up to one second of latency for events written by another process.
