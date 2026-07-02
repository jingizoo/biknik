# Persistence

The store sits behind a small repository interface. Two implementations share
that interface, selected at runtime:

| Store | Use |
| --- | --- |
| `InMemoryStore` | Demo default and fast unit tests (resets on restart) |
| `SqlStore` (PostgreSQL) | **Product target** — durable, survives restarts |
| `SqlStore` (SQLite) | Local/dev/test adapter so the SQL path runs without a Postgres server |

Selection:

```python
from hockey_scheduler.store import create_store
store = create_store()                  # DATABASE_URL env → SqlStore, else in-memory
store = create_store("postgresql://…")  # explicit Postgres
store = create_store("/path/app.db")    # explicit SQLite file
```

## Why the services needed a refactor

The service layer previously mutated dataclasses in place (`game.locked = True`)
and relied on the in-memory store holding the same object by reference. A SQL
store returns fresh rows per query, so those mutations wouldn't persist. The
store interface now includes:

- `all_*()` listings (replacing direct `.values()` access), and
- `save_*()` methods for mutable entities (game, ice slot, substitute, roster
  entry, availability).

Services call `save_*()` after every in-place mutation. For `InMemoryStore`
these are no-ops (object identity); for `SqlStore` they write the row. The
domain/service logic is otherwise unchanged.

## SqlStore design

- One portable schema; **PostgreSQL is the target, SQLite the dev/test adapter**.
  Both speak DB-API 2.0; SQL is authored with `?` placeholders and a tiny
  dialect shim translates to `%s` for psycopg.
- Portable column types only: `TEXT` and `INTEGER`. Datetimes are stored as
  ISO-8601 text, booleans as 0/1, dict payloads (audit detail) as JSON text.
- Rows ↔ dataclasses via per-table column specs with converters (enum,
  datetime, bool, json). IDs stay the existing opaque strings (`game_1`, …);
  a `counters` table makes `next_id` durable across restarts.

## Migrations (#75)

Schema changes are applied by a small forward-only runner in `migrate()`:

- Migrations live as numbered SQL files under `store/migrations/`
  (`001_initial.sql`, `002_sessions.sql`, …), applied in numeric order.
- `schema_migrations(version, applied_at)` is **authoritative**: a version
  already recorded there is skipped, and a version is recorded only after all
  of its statements succeed. The runner never drops or rewrites data — it only
  applies pending migrations.
- The DDL is `CREATE … IF NOT EXISTS`, so adopting this system on a database
  that predates it (all tables present, no per-migration rows) is safe: the
  files re-run harmlessly and backfill the ledger.
- The only destructive path is `reset_schema()` (drop + re-migrate), used by
  the demo Reset and tests. It is **never** invoked in production — the
  production boot preserves the existing database and only runs pending
  migrations forward.

## Tables

`leagues, seasons, divisions, clubs, teams, players, venues, rinks, ice_slots,
games, game_roster_entries, game_availability, substitute_enrollments,
audit_logs, notification_events, setup_audit_logs, officials,
official_assignments, game_results, notifications_feed,
notification_recipients, notification_deliveries, contact_destinations,
device_tokens, user_accounts, sessions, counters, schema_migrations`.

## Testing

- `test_sql_store.py` runs the real service/API flows and **reload** assertions
  against the SqlStore (SQLite) — proving parity and that state survives
  re-opening the database.
- `test_postgres_smoke.py` runs the same flow against **PostgreSQL**, gated on
  `TEST_DATABASE_URL`. CI provides a Postgres service and sets that variable, so
  the Postgres path is exercised on every PR; locally the test is skipped.

## Out of scope (future)

Connection pooling, async drivers, read replicas, multi-tenant row-level
security (lands with auth/RBAC, #24), and down/rollback migrations (the runner
is forward-only by design).
