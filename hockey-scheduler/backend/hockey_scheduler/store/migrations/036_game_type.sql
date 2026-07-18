-- 036_game_type: distinguish regular (standings) games from exhibitions (#283
-- Slice D).
--
-- A Game gains a competition kind. "regular" games count toward standings and
-- are bound to a single LeagueSeason — both teams must be registered in it, so
-- cross-League / cross-Program pairings are rejected (rules 8/9). "exhibition"
-- games are friendlies that may cross League lines within a Season and never
-- affect standings; they carry no owning League or Division.
--
-- Nullable-safe upgrade: a constant DEFAULT backfills every existing row to
-- 'regular' (the only kind that existed before this migration), so historical
-- games keep counting toward standings exactly as they did. Portable across
-- SQLite and PostgreSQL: a TEXT column with a constant NOT NULL DEFAULT. The
-- schema_migrations ledger guards this non-idempotent ALTER (see migrate()).

ALTER TABLE games ADD COLUMN game_type TEXT NOT NULL DEFAULT 'regular';

CREATE INDEX IF NOT EXISTS ix_games_game_type ON games(game_type);
