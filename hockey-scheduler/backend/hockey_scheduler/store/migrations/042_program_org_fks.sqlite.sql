-- Referential integrity for the competition/facility ownership hierarchy above
-- the physical facility layer (#201 Slice 4).
--
-- programs.operator_organization_id → organizations(id),
-- venues.organization_id → organizations(id), seasons.program_id → programs(id),
-- leagues.program_id → programs(id), and the legacy venues.league_id →
-- programs(id) owner link. These close the create-child-vs-delete-parent races
-- carved out of Slice 3 (jingizoo/biknik#201): create_program vs
-- delete_organization, create_venue vs delete_organization, create_season vs
-- delete_program, create_league vs delete_program, and create_venue vs
-- delete_program. Every foreign-key column stays NULLABLE (each is an optional
-- or not-declared-NOT-NULL id); only a concrete id must now name a row that
-- exists.
--
-- venues gains TWO outgoing foreign keys (organization_id → organizations,
-- league_id → programs). SQLite's "FOREIGN KEY constraint failed" names neither,
-- so the store write-site disambiguates which validated parent went missing by
-- re-reading it; PostgreSQL reports the exact constraint name on its diagnostics.
--
-- SQLite cannot add a foreign key to an existing table, so each table that GAINS
-- a foreign key is rebuilt (the standard create-copy-drop-rename) and every
-- existing index recreated. Tables are rebuilt in dependency order so each
-- child's target already exists as its rebuilt self: programs (→ the unchanged
-- organizations) BEFORE seasons/leagues/venues (→ the rebuilt programs). venues
-- also references the unchanged organizations. organizations gains no outgoing
-- foreign key and is not rebuilt.
--
-- venues IS referenced by rinks.venue_id and season_venue_access.venue_id
-- (migration 041). Dropping a still-referenced parent that already holds child
-- rows leaves a deferred foreign-key violation that PRAGMA defer_foreign_keys
-- does NOT clear when the parent is recreated, so on a populated upgrade the
-- migration runner suspends enforcement the SQLite-recommended way instead —
-- PRAGMA foreign_keys = OFF set *before* the transaction (see _apply_migration),
-- with a foreign_key_check gate before COMMIT proving the incoming
-- rinks/season_venue_access → venues references resolve cleanly against the
-- rebuilt venues. The defer pragma below still covers the nested-transaction
-- path (the demo reset re-migrating emptied tables, where foreign_keys cannot be
-- toggled mid-transaction and every table is empty). The whole rebuild runs
-- inside one transaction, so it is all-or-nothing, and the pre-migration check
-- (assert_program_org_fks_ready) validates the data first.
PRAGMA defer_foreign_keys = ON;

-- programs: add operator_organization_id → organizations(id); preserve every
-- column and both indexes. programs has no incoming foreign key, so it is
-- rebuilt first and its children (seasons/leagues/venues) target the rebuilt row.
CREATE TABLE programs_new (
  id TEXT PRIMARY KEY,
  name TEXT,
  country TEXT,
  timezone TEXT,
  operator_organization_id TEXT REFERENCES organizations (id) ON DELETE NO ACTION,
  external_ref TEXT
);
INSERT INTO programs_new
  SELECT id, name, country, timezone, operator_organization_id, external_ref
  FROM programs;
DROP TABLE programs;
ALTER TABLE programs_new RENAME TO programs;
CREATE INDEX IF NOT EXISTS ix_programs_operator_organization
  ON programs (operator_organization_id);
CREATE INDEX IF NOT EXISTS ix_programs_external_ref ON programs (external_ref);

-- seasons: add program_id → programs(id); preserve every column (incl.
-- status NOT NULL DEFAULT 'active' and archived_at from migration 039) and both
-- indexes.
CREATE TABLE seasons_new (
  id TEXT PRIMARY KEY,
  program_id TEXT REFERENCES programs (id) ON DELETE NO ACTION,
  name TEXT,
  start_date TEXT,
  end_date TEXT,
  external_ref TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  archived_at TEXT
);
INSERT INTO seasons_new
  SELECT id, program_id, name, start_date, end_date, external_ref, status,
         archived_at
  FROM seasons;
DROP TABLE seasons;
ALTER TABLE seasons_new RENAME TO seasons;
CREATE INDEX IF NOT EXISTS ix_seasons_external_ref ON seasons (external_ref);
CREATE INDEX IF NOT EXISTS ix_seasons_program ON seasons (program_id);

-- leagues: add program_id → programs(id); preserve every column and both indexes.
CREATE TABLE leagues_new (
  id TEXT PRIMARY KEY,
  name TEXT,
  sort_order INTEGER,
  external_ref TEXT,
  program_id TEXT REFERENCES programs (id) ON DELETE NO ACTION
);
INSERT INTO leagues_new
  SELECT id, name, sort_order, external_ref, program_id
  FROM leagues;
DROP TABLE leagues;
ALTER TABLE leagues_new RENAME TO leagues;
CREATE INDEX IF NOT EXISTS ix_leagues_external_ref ON leagues (external_ref);
CREATE INDEX IF NOT EXISTS ix_leagues_program ON leagues (program_id);

-- venues: add organization_id → organizations(id) AND league_id → programs(id);
-- preserve every column and both indexes. venues is referenced by
-- rinks.venue_id and season_venue_access.venue_id (migration 041) — see the
-- foreign_keys = OFF note above for how the populated-upgrade drop is handled.
CREATE TABLE venues_new (
  id TEXT PRIMARY KEY,
  name TEXT,
  address TEXT,
  timezone TEXT,
  organization_id TEXT REFERENCES organizations (id) ON DELETE NO ACTION,
  league_id TEXT REFERENCES programs (id) ON DELETE NO ACTION,
  external_ref TEXT
);
INSERT INTO venues_new
  SELECT id, name, address, timezone, organization_id, league_id, external_ref
  FROM venues;
DROP TABLE venues;
ALTER TABLE venues_new RENAME TO venues;
CREATE INDEX IF NOT EXISTS ix_venues_league ON venues (league_id);
CREATE INDEX IF NOT EXISTS ix_venues_external_ref ON venues (external_ref);

-- Post-rebuild self-check. defer_foreign_keys re-validates every deferred
-- foreign key at COMMIT and ABORTS on any violation (the authoritative
-- enforcement for the nested/empty path); foreign_key_check reports (never
-- repairs) any residual violation across the rebuilt tables and the incoming
-- rinks/season_venue_access → venues references — it yields rows rather than
-- raising, so the test suite asserts it returns none after this migration.
PRAGMA foreign_key_check;
