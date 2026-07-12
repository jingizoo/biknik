-- At most one roster row per (game, player) (#201 Slice 3B).
--
-- The service layer already keeps a single row per pair — a removed/unavailable
-- entry is revived, never duplicated (roster_service.select_roster) — but that
-- check-then-write is not safe under a cross-process race. A UNIQUE index makes
-- the invariant hold in the database. Every row counts regardless of status
-- (a removed player keeps its row), so no partial predicate is needed.
--
-- Uniqueness applies only to CONCRETE (game_id, player_id) pairs. game_id and
-- player_id are still nullable, and both SQLite and PostgreSQL treat NULLs as
-- distinct in a unique index, so a plain index would silently allow duplicate
-- NULL-bearing rows. The partial predicate makes that explicit and identical to
-- the pre-migration check, so the validation describes exactly what the index
-- enforces. Null/invalid references are out of scope here — they belong to the
-- later not-null / foreign-key relationship work (#201).
--
-- Keep the existing non-partial ix_roster_game on (game_id, player_id): it still
-- serves the per-game roster read (SqlStore.roster_for_game, WHERE game_id = ?),
-- which the PARTIAL unique index below cannot — NULL-player rows are outside the
-- partial index, so the DB couldn't use it for that scan. Once the later NOT NULL
-- migration removes nullable references, ix_roster_game becomes genuinely
-- redundant and can be dropped then.
--
-- A forward-only pre-migration check (store.integrity_checks) reports any
-- pre-existing duplicate concrete (game, player) rows before this index is
-- created, so an upgrade fails loudly rather than with an opaque index error.
CREATE UNIQUE INDEX IF NOT EXISTS ux_roster_game_player
  ON game_roster_entries (game_id, player_id)
  WHERE game_id IS NOT NULL AND player_id IS NOT NULL;
