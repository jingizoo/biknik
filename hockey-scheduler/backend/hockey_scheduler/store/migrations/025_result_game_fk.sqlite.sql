-- Referential integrity: a result must reference a real game (#201 Slice 3D).
--
-- game_results.game_id → games(id). The column stays NULLABLE (a nullable
-- foreign key still permits NULL — a result not yet tied to a game); only a
-- concrete game_id must now name a game that exists. Requiring the column
-- (NOT NULL) is a later slice.
--
-- SQLite cannot add a foreign key to an existing table, so the table is rebuilt
-- (the standard create-copy-drop-rename): a new table carrying the FK, a copy of
-- every row, then swap. The existing indexes (ix_game_results_game lookup and
-- the ux_game_result_game partial-unique from #201 Slice 3C) are recreated on
-- the new table. The whole rebuild runs inside one transaction (see
-- _apply_migration), so it is all-or-nothing. Foreign-key enforcement is ON for
-- the connection; the copy is validated by the pre-migration check first, so the
-- INSERT below cannot hit a dangling reference, and game_results is not itself
-- referenced by any other table, so the drop/rename triggers no FK actions.
CREATE TABLE game_results_new (
  id TEXT PRIMARY KEY,
  game_id TEXT REFERENCES games (id),
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
CREATE INDEX IF NOT EXISTS ix_game_results_game ON game_results (game_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_game_result_game
  ON game_results (game_id)
  WHERE game_id IS NOT NULL;
