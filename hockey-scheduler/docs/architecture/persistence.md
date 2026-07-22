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
  of its statements succeed. Migrations are forward-only and vary in kind: most
  add columns or indexes; some **backfill or transform existing row values**
  (e.g. 037 populates `game.league_season_id`); and — because SQLite cannot add a
  foreign key to an existing table — an FK migration on SQLite **rebuilds** the
  affected tables (create-copy-drop-rename; e.g. migration 040 copies every team
  and player row into a new table with the foreign key, and migration 041 does
  the same for rinks, ice slots, games, and season-venue-access rows, then drops
  the old ones). That rebuild preserves each row's values, columns, indexes
  (including the partial unique `ux_games_active_ice_slot`), and incoming
  references (`game_results`/`game_roster_entries` still target the rebuilt
  `games`). Because dropping a still-referenced parent that holds child rows
  registers a deferred foreign-key violation that `PRAGMA defer_foreign_keys` does
  not clear, the runner suspends enforcement the SQLite-recommended way for a
  populated upgrade — `PRAGMA foreign_keys = OFF` set before the migration's
  transaction, with a `foreign_key_check` gate before `COMMIT` so a genuinely
  inconsistent rebuild still fails loudly and rolls back. The whole rebuild runs
  inside the migration's single transaction and is gated by a
  fail-closed pre-migration data check — so it is all-or-nothing — but it is a
  physical table rewrite, not an in-place `ALTER`. **Take a backup before
  upgrading.**
- The DDL is `CREATE … IF NOT EXISTS`, so adopting this system on a database
  that predates it (all tables present, no per-migration rows) is safe: the
  files re-run harmlessly and backfill the ledger.
- The only destructive path is `reset_schema()` (drop + re-migrate), used by
  the demo Reset and tests. It is **never** invoked in production — the
  production boot preserves the existing database and only runs pending
  migrations forward.

## Referential integrity (#201)

Selected relationships are enforced by database foreign keys so a concurrent
delete can never strand a child row under PostgreSQL READ COMMITTED (the service
dependency checks alone are check-then-write and lose the race). The declared
foreign keys are:

- `game_results.game_id → games(id)` (migrations 025/026, later `NOT NULL`);
- `game_roster_entries.game_id → games(id)` and `.player_id → players(id)`
  (migration 027);
- `teams.club_id → clubs(id)` and `players.team_id → teams(id)` (migration 040 —
  the backstop for the assign/delete reassignment races);
- `rinks.venue_id → venues(id)`, `ice_slots.rink_id → rinks(id)`,
  `games.ice_slot_id → ice_slots(id)`, and `season_venue_access.venue_id →
  venues(id)` (migration 041 — the backstop for the facility-hierarchy "no row
  lock" races: create_rink vs delete_venue, create_ice_slot vs delete_rink,
  create_game vs delete_ice_slot, and grant_season_venue_access vs delete_venue).
  The season side of `season_venue_access` deliberately gets **no** foreign key:
  `grant_season_venue_access` and `delete_season` both take the Season row lock,
  so that pair already serialises; only the venue side lacked a lock.

Cross-Season ice-slot **double-booking** (two active games on one slot, created
under different Season locks that don't serialise) is already database-enforced
by the partial unique index `ux_games_active_ice_slot` (migration 022); migration
041 adds only the store-boundary translation of that violation into a stable
`ScheduleConflictError` (`ice_slot_taken`), so the race loser sees the same
conflict the service pre-check raises — no new constraint is needed.

Migration 041 is the **facility-hierarchy subset** of #201's no-row-lock work.
**Migration 042** (#201 Slice 4) adds the **Program/Organization + Venue-owner**
subset — the same unlocked-read → stale-write exposure one level up:

- `programs.operator_organization_id → organizations(id)` (`create_program` vs
  `delete_organization`);
- `venues.organization_id → organizations(id)` (`create_venue` vs
  `delete_organization`);
- `seasons.program_id → programs(id)` (`create_season` vs `delete_program`);
- `leagues.program_id → programs(id)` (`create_league` vs `delete_program`);
- the legacy `venues.league_id → programs(id)` owner link (`create_venue` vs
  `delete_program`).

`venues` therefore carries **two** outgoing foreign keys; a racing write's
violation is disambiguated at the store boundary by constraint name on
PostgreSQL and by re-reading which validated parent is now missing on SQLite
(whose error names neither). `delete_program` now reports the Program's permanent
Leagues as a dependency group — with `leagues.program_id` a real foreign key, a
League can no longer be silently orphaned. Because `create_league` requires a
Season (whose presence itself blocks `delete_program`), the `leagues → programs`
race is exercised at the store boundary; the other four families have forced
PostgreSQL race coverage. On SQLite, migration 042 rebuilds
programs/seasons/leagues/venues in dependency order (programs first); `venues` is
referenced by `rinks`/`season_venue_access` (migration 041), so the populated
upgrade rides the same runner `foreign_keys = OFF` + `foreign_key_check`
mechanism 041 introduced.

Together 041 and 042 close the setup-hierarchy no-row-lock structural races that
#201 catalogued.

Each such migration ships with (a) a forward-only pre-migration check in
`store/integrity_checks.py` that reports any pre-existing dangling row and aborts
the upgrade with the offending ids named, and (b) a translation in
`store/db_errors.py` so a runtime violation surfaces as a stable, secret-free
domain conflict (e.g. `team_not_found` / `club_not_found`) rather than a raw
driver error. FK columns that hold an optional link (`teams.club_id`) stay
nullable — a nullable foreign key still rejects a concrete missing parent. Delete
behaviour is spelled out explicitly as `ON DELETE NO ACTION` in both dialects
(never cascade, never data cleanup): deleting a still-referenced parent is
rejected. The in-memory store has no foreign keys; it relies on its process-wide
transaction lock plus the same service dependency checks for parity.

For the no-row-lock deletes (`delete_venue`/`delete_rink`/`delete_ice_slot` from
migration 041, and `delete_organization`/`delete_program` from migration 042),
losing the race must still return the operator the **same itemised
has-dependencies error** the pre-check produces — the concurrently-committed
dependent named with its group/count/ids — not a thin timing error. Because the losing `DELETE` aborts the
PostgreSQL transaction, that re-resolution can only read a clean connection *after*
the transaction has rolled back. The store therefore raises an internal
`DependentDeleteConflict` at the delete site and re-resolves it from the
**outermost** `transaction()`'s post-rollback handler (a service callback
registered via `set_dependent_conflict_resolver`). This is correct whether the
delete ran on its own or was nested inside a caller's `with store.transaction():`
(the service delete joins that outer unit without a savepoint): the whole outer
unit rolls back with zero partial state, and the re-scan never runs on the
still-aborted connection — so a nested delete never leaks `InFailedSqlTransaction`
or poisons the caller's atomic unit.

**Rollback / recovery (forward-only).** These migrations are forward-only; there
is no down-migration. On SQLite these FK migrations physically rebuild their
tables (migration 040: `teams`/`players`; migration 041:
`rinks`/`ice_slots`/`games`/`season_venue_access`; migration 042:
`programs`/`seasons`/`leagues`/`venues` — see the migration runner note
above), so **a backup taken before the upgrade is the recovery path** — rollback
means restore-from-backup (or a future explicit down-migration), not re-running
the runner. Logical row values are preserved by the rebuild, but the physical
tables are replaced. On PostgreSQL these migrations are `ADD CONSTRAINT`
statements (no table rewrite); reverting would be an explicit `DROP CONSTRAINT`
migration. In all cases the fail-closed
pre-migration check refuses to touch a database that still holds dangling
references, so a dirty upgrade aborts cleanly with the offending ids named and
zero mutation.

### Idempotency keys for externally-retried writes (#201)

The epic requires every externally-retried write to be safe under retry via
**either a natural unique key or an idempotency key**. Most writes already carry
a natural key: a duplicate create collides on a UNIQUE index (`create_game` on
`ux_games_active_ice_slot`, one result per game, one registration per
`(team, league-season)`, …), a state transition is a no-op (`publish_game`,
`approve_result`), or an import re-applies by its external `code`. What remains
are the opaque-id entity creates, which mint a fresh `next_id(...)` on every
call and so **duplicate on a client retry**.

For those, a caller sends an `Idempotency-Key` request header. The write runs at
most once per `(actor, key)` and every retry replays the first response. The
record lives in `idempotency_keys` (`key_hash` UNIQUE), written **in the same
transaction as the create**, so a crash can never leave a committed write
without its key. `key_hash` = SHA-256 of the per-actor scope + the client key
(one caller's key can never replay another's write); `fingerprint` = SHA-256 of
the endpoint + arguments, so re-using a key for a *different* request is refused
with `idempotency_conflict` rather than returning the wrong resource. Concurrency
is covered on every backend at once: an in-transaction re-read catches a winner
already committed under the in-memory/SQLite serialized-writer lock, and the
`UNIQUE(key_hash)` index makes two concurrent PostgreSQL retries race on the
insert — the loser rolls its duplicate back and replays. This is the same
insert-first pattern `InstallationState` uses for the first-admin claim.

The mechanism is wired through two representative creates so far —
`create_venue` and `create_ice_slot` — with the remaining opaque-id creates
(program/season/league/division/club/team/rink/organization/official, plus the
calendar-feed-token / reschedule / official-availability outliers) a tracked
follow-up.

## Tables

`leagues, seasons, divisions, clubs, teams, players, venues, rinks, ice_slots,
games, game_roster_entries, game_availability, substitute_enrollments,
audit_logs, notification_events, setup_audit_logs, officials,
official_assignments, game_results, notifications_feed,
notification_recipients, notification_deliveries, contact_destinations,
device_tokens, user_accounts, sessions, idempotency_keys, counters,
schema_migrations`.

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
