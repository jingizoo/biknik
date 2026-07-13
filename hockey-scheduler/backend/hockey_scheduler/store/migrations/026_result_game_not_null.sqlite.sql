-- Require a game for every result + finalize the uniqueness index (#201 Slice 3E).
--
-- Completes the result → game relationship that Slice 3D (#229) started: 3D added
-- the foreign key but left game_id nullable and deferred requiring it. Now every
-- result must name a game.
--
-- SQLite can't alter a column to NOT NULL in place, so the table is rebuilt (the
-- standard create-copy-drop-rename) with game_id declared NOT NULL and carrying
-- the foreign key. With game_id NOT NULL the Slice 3C partial predicate is vacuous
-- and the separate non-unique lookup index (ix_game_results_game) is redundant, so
-- the rebuild creates a single plain UNIQUE index on game_id — it both enforces
-- one-result-per-game and serves the per-game read (result_for_game). The old
-- indexes are dropped with the old table.
--
-- The whole rebuild runs inside one transaction (see _apply_migration), so it is
-- all-or-nothing. FK enforcement is on; the copy is validated by the pre-migration
-- check first (no NULL game_ids, so the NOT NULL column accepts every row), and
-- game_results is not referenced by any other table, so the drop/rename triggers
-- no FK actions.
CREATE TABLE game_results_new (
  id TEXT PRIMARY KEY,
  game_id TEXT NOT NULL REFERENCES games (id),
  home_score INTEGER,
  away_score INTEGER,
  status TEXT,
  recorded_by TEXT,
  recorded_at TEXT,
  approved_by TEXT,
  approved_at TEXT
);
INSERT INTO game_results_new
  SELECT id, game_id, home_score, away_score, status,
         recorded_by, recorded_at, approved_by, approved_at
  FROM game_results;
DROP TABLE game_results;
ALTER TABLE game_results_new RENAME TO game_results;
CREATE UNIQUE INDEX ux_game_result_game ON game_results (game_id);
