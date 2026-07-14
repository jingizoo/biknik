-- 028_competition_reset: canonical competition-model rename + forward-only
-- reparent (#233 Slice C1b).
--
-- Renames the umbrella `leagues` table to `programs` (and its owner column to
-- operator_organization_id), promotes the old `levels` grouping to the new
-- `leagues`, reparents every division/registration/game onto a competition
-- League, and adds the derived league_id to registrations and games. Forward
-- only and id-stable: no row is deleted and no id changes.
--
-- ORDER MATTERS (name collision): the umbrella `leagues` must become `programs`
-- BEFORE the old `levels` becomes `leagues`, or the second rename would collide.
--
-- SQLite >= 3.25 expresses every step with ALTER TABLE RENAME TO / RENAME COLUMN
-- / ADD COLUMN + UPDATE, so no table rebuild is needed. A pre-migration gate
-- (assert_competition_reset_ready) proves every division and registration maps
-- deterministically onto a single same-Season League before these run, so each
-- backfill subquery below resolves to exactly one row.

-- 1. leagues -> programs; organization_id -> operator_organization_id.
DROP INDEX IF EXISTS ix_leagues_organization;
DROP INDEX IF EXISTS ix_leagues_external_ref;
ALTER TABLE leagues RENAME TO programs;
ALTER TABLE programs RENAME COLUMN organization_id TO operator_organization_id;
CREATE INDEX IF NOT EXISTS ix_programs_operator_organization ON programs(operator_organization_id);
CREATE INDEX IF NOT EXISTS ix_programs_external_ref ON programs(external_ref);

-- 2. seasons.league_id -> program_id.
ALTER TABLE seasons RENAME COLUMN league_id TO program_id;
CREATE INDEX IF NOT EXISTS ix_seasons_program ON seasons(program_id);

-- 3. teams.league_id -> program_id (ix_teams_league -> ix_teams_program).
DROP INDEX IF EXISTS ix_teams_league;
ALTER TABLE teams RENAME COLUMN league_id TO program_id;
CREATE INDEX IF NOT EXISTS ix_teams_program ON teams(program_id);

-- 4. levels -> leagues (promote the grouping to its new name).
DROP INDEX IF EXISTS ix_levels_external_ref;
ALTER TABLE levels RENAME TO leagues;
CREATE INDEX IF NOT EXISTS ix_leagues_external_ref ON leagues(external_ref);

-- 5. divisions.level_id -> league_id, then backfill a league for every division
--    that has none from its season's sole league (the gate guarantees exactly
--    one such league per affected season).
ALTER TABLE divisions RENAME COLUMN level_id TO league_id;
UPDATE divisions SET league_id = (
  SELECT lv.id FROM leagues lv WHERE lv.season_id = divisions.season_id)
  WHERE league_id IS NULL;

-- 6. season_team_registrations: add league_id, backfill from the division's
--    league when the registration has one, else the season's sole league.
ALTER TABLE season_team_registrations ADD COLUMN league_id TEXT;
UPDATE season_team_registrations SET league_id = (
  SELECT d.league_id FROM divisions d
  WHERE d.id = season_team_registrations.division_id)
  WHERE league_id IS NULL
    AND division_id IS NOT NULL
    AND (SELECT d.league_id FROM divisions d
         WHERE d.id = season_team_registrations.division_id) IS NOT NULL;
UPDATE season_team_registrations SET league_id = (
  SELECT lv.id FROM leagues lv
  WHERE lv.season_id = season_team_registrations.season_id)
  WHERE league_id IS NULL;

-- 7. games: add league_id, backfill from the game's division league.
ALTER TABLE games ADD COLUMN league_id TEXT;
UPDATE games SET league_id = (
  SELECT d.league_id FROM divisions d WHERE d.id = games.division_id)
  WHERE division_id IS NOT NULL;
