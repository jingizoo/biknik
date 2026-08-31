-- 058_athlete_identity: durable athlete identity + versioned age eligibility (#273).
--
-- NUMBERING NOTE (parallel lanes, #424 owner review): originally authored as
-- 051_athlete_identity against a main whose own migrations stopped at 050.
-- By the time this branch's PR reached review, main had independently
-- advanced through 057 (051_active_context_generation, 052_epoch_fence_
-- version, 053_season_copy_forward_commits, 054_copy_forward_response_
-- snapshot, 055_copy_forward_request_identity, and #426's own renumbered
-- 056_data_access_log / 057_device_token_unique_key) -- a same-leading-
-- number collision with main's OWN 051 file (different filename, so git's
-- merge saw no textual conflict and both files coexisted in migrations/).
-- That was not a mere cosmetic collision: on a database already migrated
-- through main's 057 BEFORE this file ever existed, migrate() applies this
-- file chronologically LAST (never previously recorded, so it lands
-- whenever the loop first reaches it) -- but on a FRESH database the same
-- file sorts and applies BEFORE 052-057, since "051_athlete_identity" <
-- "052_epoch_fence_version" lexicographically. Two different real DDL-
-- execution orders for the same file depending on install history,
-- reproduced empirically (SQLite + PostgreSQL) against the real migrate()/
-- _load_migrations() before this rename. Resolved the same way #426's own
-- 053/055->056/057 collision was: renumbering past main's claimed range, to
-- 058. No functional statement below changed; only the version stem
-- (filename, and its references in the identity-migration-upgrade tests'
-- ``_MIGRATION``/helper/method names, test_reassignment_fks.py's replay
-- assertion, and the "migration 051" prose naming this migration across
-- domain/models.py, services/setup_service.py, store/db_errors.py,
-- store/sql_store.py, and the competition-reset/age-eligibility/
-- registration-number tests) moved from 051 to 058.
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
--
-- #273 review round 2 finding 3: every column the service always populates
-- (id/league_season_id/version/cutoff_month/cutoff_day/tiers/enforcement/
-- created_at — set_age_eligibility_rule never constructs a row without all
-- seven) is NOT NULL, matching migration 050's schedule_scenarios precedent
-- for an immutable versioned-row table. ``actor_id`` stays nullable, like
-- every other audit-adjacent actor column in this schema — an unattributed
-- system write is legitimate. ``league_season_id`` carries a FOREIGN KEY to
-- league_seasons(id): a rule can no longer be inserted against (or survive
-- under, on a race — see delete_league_season below) a LeagueSeason id that
-- does not exist, and the deliberately unspecified ON DELETE action is the
-- default RESTRICT/NO ACTION — a still-referenced LeagueSeason cannot be
-- deleted out from under its rule history. This is a DB-level backstop for
-- the SAME invariant delete_league_season's dependency gate now also
-- enforces at the service layer (rule rows are included in its itemized
-- dependents), not a substitute for it: the gate gives an operator an
-- itemized, friendly refusal; the FK is what makes the invariant hold even
-- under a create-rule-vs-delete-binding race. Defined at CREATE TABLE time
-- (this table is new in this same migration), so — unlike migration 040's
-- FKs onto pre-existing tables — no SQLite rebuild is needed; the DDL below
-- is portable to both engines as-is.
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

-- At most one Player per (team, registration number) — REGARDLESS of active
-- state (#273 review round 2 finding 2). Unlike jersey numbers (migration
-- 038, active-only) a registration number is a governing-body identity, and
-- SetupService._assert_registration_number_available refuses a same-team
-- duplicate on an INACTIVE row too: deactivating a player must not free the
-- identity for an accidental duplicate. The service layer already rejects a
-- same-team duplicate at create/update/move (assign_player_team) and both
-- import paths, but that check-then-write is not safe under a cross-process
-- race; a partial UNIQUE index makes the invariant hold in the database too,
-- so two concurrent writers can never both land the same (team, registration)
-- pair. The loser's UNIQUE violation is translated at the store boundary into
-- the same stable conflict the service pre-check raises (store.db_errors,
-- reason duplicate_registration_number), never a raw driver error.
--
-- registration_number IS NOT NULL — a player may have none, and any number of
-- a team's players may share "no registration number"; only concrete values
-- are unique. Portable across SQLite and PostgreSQL: both support partial
-- unique indexes (as migration 038 already relies on).
CREATE UNIQUE INDEX IF NOT EXISTS ux_players_team_registration_number
    ON players (team_id, registration_number)
    WHERE registration_number IS NOT NULL;

CREATE TABLE IF NOT EXISTS age_eligibility_rules (
    id TEXT PRIMARY KEY,
    league_season_id TEXT NOT NULL REFERENCES league_seasons(id),
    version INTEGER NOT NULL,
    cutoff_month INTEGER NOT NULL,
    cutoff_day INTEGER NOT NULL,
    tiers TEXT NOT NULL,
    enforcement TEXT NOT NULL,
    created_at TEXT NOT NULL,
    actor_id TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_age_rules_league_season_version
    ON age_eligibility_rules (league_season_id, version);
