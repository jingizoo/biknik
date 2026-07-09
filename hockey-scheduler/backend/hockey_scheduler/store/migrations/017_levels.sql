-- 017_levels: competitive level/tier between Season and Division (#166 PR B).
--
-- A Level groups divisions inside a season (e.g. "Level 1" holding "Div A" and
-- "Div B"). Divisions link up via a nullable level_id, so every pre-existing
-- division simply has no level until one is assigned — no backfill required.
-- Portable across SQLite and PostgreSQL: TEXT/INTEGER columns only. The
-- schema_migrations ledger guards the non-idempotent ALTER (see migrate()).

CREATE TABLE IF NOT EXISTS levels (
  id TEXT PRIMARY KEY,
  season_id TEXT,
  name TEXT,
  sort_order INTEGER
);

ALTER TABLE divisions ADD COLUMN level_id TEXT;
