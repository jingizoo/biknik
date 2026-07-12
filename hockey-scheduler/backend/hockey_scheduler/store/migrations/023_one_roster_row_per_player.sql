-- At most one roster row per (game, player) (#201 Slice 3B).
--
-- The service layer already keeps a single row per pair — a removed/unavailable
-- entry is revived, never duplicated (roster_service.select_roster) — but that
-- check-then-write is not safe under a cross-process race. A UNIQUE index makes
-- the invariant hold in the database. Every row counts regardless of status
-- (a removed player keeps its row), so no partial predicate is needed.
--
-- Replaces the redundant non-unique ix_roster_game on the same columns (it
-- served the same (game_id, player_id) lookups; the unique index covers them).
-- A forward-only pre-migration check (store.integrity_checks) reports any
-- pre-existing duplicate (game, player) rows before this index is created, so an
-- upgrade fails loudly rather than with an opaque index error.
DROP INDEX IF EXISTS ix_roster_game;
CREATE UNIQUE INDEX IF NOT EXISTS ux_roster_game_player
  ON game_roster_entries (game_id, player_id);
