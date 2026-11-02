# ADR 0002: SQLite by default, PostgreSQL through DATABASE_URL

- Status: Accepted
- Date: 2026-09-23

## Context

Local-first means zero setup: no database server for a solo developer. Teams and remote
workers need a server database. Both must run the same schema and queries.

## Decision

- Default database: SQLite in the data directory (`<data_dir>/agenttape.db`).
- Every SQLite connection sets `journal_mode=WAL`, `busy_timeout=10000`,
  `foreign_keys=ON`, and `synchronous=NORMAL`.
- `DATABASE_URL` switches to PostgreSQL (or anything SQLAlchemy supports).
- Queries stay portable: no SQLite-only SQL, no `INSERT OR IGNORE`, no JSON path
  operators in filters. Races are handled with conditional `UPDATE ... WHERE` plus
  `rowcount` checks and unique constraints, which behave the same on both engines.
- JSON columns use `JSON` on SQLite and `JSONB` on PostgreSQL; timestamps use a
  `UTCDateTime` type that stores naive UTC text on SQLite (so lexical order is time
  order) and `timestamptz` on PostgreSQL. Python code only sees aware UTC datetimes.
- Alembic owns the schema. Upgrades always target `heads` so parallel branches of
  revisions (from parallel branches of work) still apply before they are merged.
- A migration smoke test (upgrade, downgrade to base, upgrade, then an autogenerate
  diff against the models) runs in `make check`.

## Consequences

- SQLite allows one writer at a time; WAL plus a busy timeout make that invisible at
  local scale. Heavy multi-worker setups should use PostgreSQL.
- The PostgreSQL driver is not bundled; users add `psycopg` when they need it. CI for
  PostgreSQL is documented rather than run by default.
