-- Referential integrity for the physical facility hierarchy (#201 Slice 3).
--
-- rinks.venue_id → venues(id), ice_slots.rink_id → rinks(id),
-- games.ice_slot_id → ice_slots(id), and season_venue_access.venue_id →
-- venues(id). These close the reviewer-catalogued "no row lock" races handed to
-- #201: create_rink vs delete_venue, create_ice_slot vs delete_rink, create_game
-- vs delete_ice_slot, and grant_season_venue_access vs delete_venue. Every
-- foreign-key column stays NULLABLE (a nullable foreign key still permits NULL;
-- season_venue_access.venue_id is independently NOT NULL from migration 029);
-- only a concrete id must now name a row that exists.
--
-- The season side of season_venue_access gets no foreign key here: grant and
-- delete_season both take the Season row lock, so that pair already serializes;
-- only the venue side lacks a lock. Keeping it to one outgoing foreign key also
-- lets the store write-site disambiguate SQLite's single FOREIGN KEY error.
--
-- Cross-Season ice-slot double-booking is already DB-enforced by migration 022's
-- partial unique index ux_games_active_ice_slot; this slice only adds its
-- store-boundary translation (→ ScheduleConflictError), no new constraint.
--
-- SQLite cannot add a foreign key to an existing table, so each table is rebuilt
-- (the standard create-copy-drop-rename) with its new foreign key, then every
-- existing index is recreated. Tables are rebuilt in dependency order so each
-- child's foreign-key target already exists as its rebuilt self: rinks (→ the
-- unchanged venues) BEFORE ice_slots (→ rinks) BEFORE games (→ ice_slots);
-- season_venue_access (→ venues) is independent.
--
-- games IS referenced by game_results.game_id (migration 025/026) and
-- game_roster_entries.game_id (migration 027). Dropping a still-referenced parent
-- that already holds child rows leaves a deferred foreign-key violation that
-- PRAGMA defer_foreign_keys does NOT clear when the parent is recreated, so on a
-- populated upgrade the migration runner suspends enforcement the
-- SQLite-recommended way instead — PRAGMA foreign_keys = OFF set *before* the
-- transaction (see _apply_migration), with a foreign_key_check gate before COMMIT
-- proving the incoming game_results / game_roster_entries references resolve
-- cleanly against the rebuilt games. The defer pragma below still covers the
-- nested-transaction path (the demo reset re-migrating emptied tables, where
-- foreign_keys cannot be toggled mid-transaction and every table is empty). The
-- whole rebuild runs inside one transaction, so it is all-or-nothing, and the
-- pre-migration check (assert_iceslot_venue_fks_ready) validates the data first.
PRAGMA defer_foreign_keys = ON;

-- rinks: add venue_id → venues(id); preserve every column (incl. external_ref
-- from migration 011). No secondary indexes exist on rinks.
CREATE TABLE rinks_new (
  id TEXT PRIMARY KEY,
  venue_id TEXT REFERENCES venues (id) ON DELETE NO ACTION,
  name TEXT,
  external_ref TEXT
);
INSERT INTO rinks_new
  SELECT id, venue_id, name, external_ref
  FROM rinks;
DROP TABLE rinks;
ALTER TABLE rinks_new RENAME TO rinks;

-- ice_slots: add rink_id → rinks(id); preserve every column and the
-- (rink_id, start_time) lookup index.
CREATE TABLE ice_slots_new (
  id TEXT PRIMARY KEY,
  rink_id TEXT REFERENCES rinks (id) ON DELETE NO ACTION,
  start_time TEXT,
  end_time TEXT,
  slot_type TEXT,
  status TEXT
);
INSERT INTO ice_slots_new
  SELECT id, rink_id, start_time, end_time, slot_type, status
  FROM ice_slots;
DROP TABLE ice_slots;
ALTER TABLE ice_slots_new RENAME TO ice_slots;
CREATE INDEX IF NOT EXISTS ix_ice_slots_rink ON ice_slots (rink_id, start_time);

-- games: add ice_slot_id → ice_slots(id); preserve every column (nullability +
-- game_type's NOT NULL DEFAULT 'regular') and all four secondary indexes,
-- crucially the partial unique ux_games_active_ice_slot (migration 022) that
-- enforces one active game per ice slot. Column list/order mirror the Game
-- dataclass spec exactly.
CREATE TABLE games_new (
  id TEXT PRIMARY KEY,
  home_team_id TEXT,
  start_time TEXT,
  target_goalies INTEGER,
  target_skaters INTEGER,
  max_skaters INTEGER,
  away_team_id TEXT,
  rink TEXT,
  end_time TEXT,
  roster_lock_time TEXT,
  locked INTEGER,
  cancelled INTEGER,
  published INTEGER,
  season_id TEXT,
  division_id TEXT,
  ice_slot_id TEXT REFERENCES ice_slots (id) ON DELETE NO ACTION,
  is_draft INTEGER,
  league_id TEXT,
  game_type TEXT NOT NULL DEFAULT 'regular',
  league_season_id TEXT
);
INSERT INTO games_new
  SELECT id, home_team_id, start_time, target_goalies, target_skaters,
         max_skaters, away_team_id, rink, end_time, roster_lock_time, locked,
         cancelled, published, season_id, division_id, ice_slot_id, is_draft,
         league_id, game_type, league_season_id
  FROM games;
DROP TABLE games;
ALTER TABLE games_new RENAME TO games;
CREATE INDEX IF NOT EXISTS ix_games_slot ON games (ice_slot_id);
CREATE INDEX IF NOT EXISTS ix_games_teams ON games (home_team_id, away_team_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_games_active_ice_slot
  ON games (ice_slot_id) WHERE cancelled = 0 AND ice_slot_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_games_game_type ON games (game_type);
CREATE INDEX IF NOT EXISTS ix_games_league_season ON games (league_season_id);

-- season_venue_access: add venue_id → venues(id); preserve NOT NULL columns, the
-- active default, the partial-unique one-active-row index, and both lookups.
CREATE TABLE season_venue_access_new (
  id TEXT PRIMARY KEY,
  season_id TEXT NOT NULL,
  venue_id TEXT NOT NULL REFERENCES venues (id) ON DELETE NO ACTION,
  active INTEGER NOT NULL DEFAULT 1
);
INSERT INTO season_venue_access_new
  SELECT id, season_id, venue_id, active
  FROM season_venue_access;
DROP TABLE season_venue_access;
ALTER TABLE season_venue_access_new RENAME TO season_venue_access;
CREATE UNIQUE INDEX IF NOT EXISTS ux_season_venue_access_active
  ON season_venue_access (season_id, venue_id) WHERE active = 1;
CREATE INDEX IF NOT EXISTS ix_season_venue_access_season
  ON season_venue_access (season_id);
CREATE INDEX IF NOT EXISTS ix_season_venue_access_venue
  ON season_venue_access (venue_id);

-- Post-rebuild self-check. defer_foreign_keys re-validates every deferred
-- foreign key at COMMIT and ABORTS the migration transaction on any violation
-- (the authoritative enforcement, given the pre-migration data check has already
-- confirmed the rows are clean). foreign_key_check reports (never repairs) any
-- residual violation across the freshly rebuilt tables and the incoming
-- game_results/game_roster_entries → games references; it yields rows rather than
-- raising, so the test suite asserts it returns none after this migration.
PRAGMA foreign_key_check;
