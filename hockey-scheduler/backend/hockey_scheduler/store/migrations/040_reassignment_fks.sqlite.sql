-- Referential integrity for reassignment writes: a player belongs to a real
-- team, and a team belongs to a real club (#201 Slice 2).
--
-- players.team_id → teams(id) and teams.club_id → clubs(id). These close the
-- reviewer-flagged reassignment races handed to #201 (assign_player_team vs
-- delete_team, assign_team_club vs delete_club, add_player onto a deleting team).
-- Both columns stay NULLABLE (a nullable foreign key still permits NULL); only a
-- concrete id must now name a row that exists. teams.club_id is legitimately
-- optional (#233 Slice D); requiring players.team_id is out of scope here.
--
-- SQLite cannot add a foreign key to an existing table, so each table is rebuilt
-- (the standard create-copy-drop-rename) with its new foreign key, then every
-- existing index is recreated on the new table. teams is rebuilt BEFORE players,
-- because players' new foreign key targets teams. players IS referenced by
-- game_roster_entries (migration 027's fk_roster_player), so the drop/recreate of
-- players is done with PRAGMA defer_foreign_keys = ON: PRAGMA foreign_keys is a
-- no-op mid-transaction, and migrations run inside a transaction, so deferral is
-- the only way to hold enforcement until COMMIT — by which point players has been
-- recreated with the same ids and the roster foreign key resolves cleanly again.
-- The whole rebuild runs inside one transaction (see _apply_migration), so it is
-- all-or-nothing. The pre-migration check (assert_reassignment_fks_ready)
-- validates the data first, so the new foreign keys reject nothing on upgrade.
PRAGMA defer_foreign_keys = ON;

-- teams: add club_id → clubs(id); preserve every column and index
-- (ix_teams_program, ix_teams_league).
CREATE TABLE teams_new (
  id TEXT PRIMARY KEY,
  name TEXT,
  division TEXT,
  club_id TEXT REFERENCES clubs (id) ON DELETE NO ACTION,
  division_id TEXT,
  external_ref TEXT,
  program_id TEXT,
  league_id TEXT
);
INSERT INTO teams_new
  SELECT id, name, division, club_id, division_id, external_ref, program_id,
         league_id
  FROM teams;
DROP TABLE teams;
ALTER TABLE teams_new RENAME TO teams;
CREATE INDEX IF NOT EXISTS ix_teams_program ON teams (program_id);
CREATE INDEX IF NOT EXISTS ix_teams_league ON teams (league_id);

-- players: add team_id → teams(id); preserve every column and index
-- (ix_players_team lookup and the 038 partial-unique active-jersey index).
CREATE TABLE players_new (
  id TEXT PRIMARY KEY,
  team_id TEXT REFERENCES teams (id) ON DELETE NO ACTION,
  name TEXT,
  position TEXT,
  shoots TEXT,
  jersey_number INTEGER,
  is_active INTEGER,
  guardian_person_id TEXT,
  external_ref TEXT
);
INSERT INTO players_new
  SELECT id, team_id, name, position, shoots, jersey_number, is_active,
         guardian_person_id, external_ref
  FROM players;
DROP TABLE players;
ALTER TABLE players_new RENAME TO players;
CREATE INDEX IF NOT EXISTS ix_players_team ON players (team_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_players_active_team_jersey
  ON players (team_id, jersey_number)
  WHERE is_active = 1 AND jersey_number IS NOT NULL;

-- Post-rebuild self-check. defer_foreign_keys re-validates every deferred
-- foreign key at COMMIT and ABORTS the migration transaction on any violation
-- (the authoritative enforcement, given the pre-migration data check has already
-- confirmed the rows are clean). foreign_key_check reports (never repairs) any
-- residual violation across the freshly rebuilt teams/players and the incoming
-- game_roster_entries → players reference; it is a pragma that yields rows rather
-- than raising, so the test suite asserts it returns none after this migration.
PRAGMA foreign_key_check;
