-- 051_athlete_identity: durable athlete identity + versioned age eligibility (#273).
--
-- Players gain structured names, a private birthdate (ISO "YYYY-MM-DD" text),
-- a stable governing-body registration number, and the 1-7 skill rating the
-- owner's #287 ruling assigned to #273. All nullable: existing rows are valid
-- as-is, and migration NEVER guesses a first/last split from the flattened
-- display name (per #273, identity data is never fabricated from a name).
--
-- ``players.guardian_person_id`` is deliberately NOT dropped here even though
-- #273 removes it from the model/contract (GuardianLink is the real guardian
-- relationship and nothing ever read this column): leaving the column in
-- place keeps this migration purely additive and keeps the frozen 040 SQLite
-- table rebuild (which copies it by name) valid on every upgrade path.
-- Dropping the dead column is an explicit follow-up migration.
--
-- ``age_eligibility_rules``: immutable, versioned cutoff/tier rules per
-- LeagueSeason. ``tiers`` is a JSON array of {"code", "max_age"} objects.
-- (version uniqueness per league_season is enforced by the unique index; rows
-- are only ever inserted with the next version by the service, inside a
-- transaction.)
ALTER TABLE players ADD COLUMN first_name TEXT;
ALTER TABLE players ADD COLUMN last_name TEXT;
ALTER TABLE players ADD COLUMN preferred_name TEXT;
ALTER TABLE players ADD COLUMN birthdate TEXT;
ALTER TABLE players ADD COLUMN registration_number TEXT;
ALTER TABLE players ADD COLUMN skill_rating INTEGER;

-- Duplicate detection scans by registration number (never by name alone);
-- NOT unique in this slice — cross-team duplicates are a WARNING while the
-- legacy permanent Player->Team model still forces one row per team on
-- multi-program athletes (#205 fixes the model; hard uniqueness is policy
-- work behind that).
CREATE INDEX IF NOT EXISTS ix_players_registration_number
    ON players (registration_number);

CREATE TABLE IF NOT EXISTS age_eligibility_rules (
    id TEXT PRIMARY KEY,
    league_season_id TEXT,
    version INTEGER,
    cutoff_month INTEGER,
    cutoff_day INTEGER,
    tiers TEXT,
    enforcement TEXT,
    created_at TEXT,
    actor_id TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_age_rules_league_season_version
    ON age_eligibility_rules (league_season_id, version);
