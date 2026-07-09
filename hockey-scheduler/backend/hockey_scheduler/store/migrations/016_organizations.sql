-- 016_organizations: facility owner/operator above Venue (#166 PR A).
--
-- An Organization owns venues (a rink company owning several arenas). It is
-- deliberately separate from Club (which owns hockey teams). Venues link up
-- via a nullable organization_id, so every pre-existing venue simply has no
-- organization until one is assigned — no backfill required. Portable across
-- SQLite and PostgreSQL: TEXT columns only. The schema_migrations ledger
-- guards against re-running the non-idempotent ALTER (see migrate()).

CREATE TABLE IF NOT EXISTS organizations (
  id TEXT PRIMARY KEY,
  name TEXT,
  short_name TEXT
);

ALTER TABLE venues ADD COLUMN organization_id TEXT;
