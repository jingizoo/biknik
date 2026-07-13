-- Referential integrity: a roster entry references a real game and player
-- (#201 Slice 3F).
--
-- game_roster_entries.game_id → games(id) and .player_id → players(id). Both
-- columns stay NULLABLE (a nullable foreign key still permits NULL); only a
-- concrete game_id/player_id must now name a row that exists. Requiring the
-- columns (NOT NULL) and collapsing the Slice 3B indexes is a later slice.
--
-- SQLite cannot add a foreign key to an existing table, so the table is rebuilt
-- (the standard create-copy-drop-rename) with both foreign keys, then the Slice
-- 3B indexes (ix_roster_game lookup and the ux_roster_game_player partial unique)
-- are recreated on the new table. The whole rebuild runs inside one transaction
-- (see _apply_migration), so it is all-or-nothing. FK enforcement is on; the copy
-- is validated by the pre-migration check first (so the new FKs reject nothing),
-- and game_roster_entries is not referenced by any other table, so the drop/rename
-- triggers no FK actions.
CREATE TABLE game_roster_entries_new (
  id TEXT PRIMARY KEY,
  game_id TEXT REFERENCES games (id),
  player_id TEXT REFERENCES players (id),
  roster_role TEXT,
  selection_source TEXT,
  status TEXT,
  selected_at TEXT,
  updated_at TEXT,
  selected_by TEXT
);
INSERT INTO game_roster_entries_new
  SELECT id, game_id, player_id, roster_role, selection_source, status,
         selected_at, updated_at, selected_by
  FROM game_roster_entries;
DROP TABLE game_roster_entries;
ALTER TABLE game_roster_entries_new RENAME TO game_roster_entries;
CREATE INDEX IF NOT EXISTS ix_roster_game
  ON game_roster_entries (game_id, player_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_roster_game_player
  ON game_roster_entries (game_id, player_id)
  WHERE game_id IS NOT NULL AND player_id IS NOT NULL;
