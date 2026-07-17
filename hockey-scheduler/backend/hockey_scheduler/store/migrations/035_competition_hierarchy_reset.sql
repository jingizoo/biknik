-- 035_competition_hierarchy_reset: Program -> League -> Team with a Season
-- overlay via LeagueSeason (#283 Slice A). Forward-only; no historical Game,
-- registration, or standings row is rewritten beyond reparenting a foreign key
-- onto the merged permanent League it already meant.
--
-- Today a `leagues` row is per-Season (Platinum-in-Winter and Platinum-in-
-- Summer are separate rows, each under a Season). This migration makes League a
-- PERMANENT child of a Program and expresses each Season's participation as a
-- `league_seasons` row. Per-Season league rows that mean the same permanent
-- League are MERGED by (program_id, coalesce(external_ref, lower(name))); the
-- lowest id in each group is the canonical permanent League, and the others are
-- deleted after every division/registration/game is reparented onto it.
--
-- A pre-migration gate (assert_competition_hierarchy_reset_ready) proves every
-- Team maps to a single League (or has an operator decision in
-- team_league_migration_decisions), and that the merge introduces no duplicate
-- LeagueSeason or registration, BEFORE any statement here runs. Because a
-- failing gate is not recorded in schema_migrations, an operator can enter the
-- missing decisions and this migration re-runs and applies on the next startup.
--
-- All columns are added nullable (matching program_id's existing pattern); the
-- service layer enforces the NOT-NULL invariants on new writes.

-- 1. LeagueSeason: a permanent League's participation in one Season.
CREATE TABLE IF NOT EXISTS league_seasons (
  id TEXT PRIMARY KEY,
  league_id TEXT NOT NULL,
  season_id TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_league_season ON league_seasons(league_id, season_id);

-- 2. leagues gains a permanent program_id, derived from the league's Season.
ALTER TABLE leagues ADD COLUMN program_id TEXT;
UPDATE leagues SET program_id = (
  SELECT s.program_id FROM seasons s WHERE s.id = leagues.season_id);

-- 3. One LeagueSeason per existing (per-Season) league row: pointing at the
--    canonical permanent League for its (program_id, key) group and the row's
--    own Season. Deterministic id 'ls_'<original league id> so a reparent can
--    address it directly. The gate proves no two rows collide on
--    (canonical, season).
INSERT INTO league_seasons (id, league_id, season_id)
SELECT 'ls_' || lg.id,
       (SELECT MIN(l2.id) FROM leagues l2
        WHERE l2.program_id = lg.program_id
          AND COALESCE(l2.external_ref, LOWER(l2.name))
              = COALESCE(lg.external_ref, LOWER(lg.name))),
       lg.season_id
FROM leagues lg;

-- 4. Divisions -> the LeagueSeason for their own per-Season league (which is in
--    the division's Season), else the Season's sole LeagueSeason when the
--    division carried no league.
ALTER TABLE divisions ADD COLUMN league_season_id TEXT;
UPDATE divisions SET league_season_id = 'ls_' || league_id
  WHERE league_id IS NOT NULL
    AND EXISTS (SELECT 1 FROM league_seasons ls
                WHERE ls.id = 'ls_' || divisions.league_id);
UPDATE divisions SET league_season_id = (
  SELECT ls.id FROM league_seasons ls WHERE ls.season_id = divisions.season_id)
  WHERE league_season_id IS NULL
    AND (SELECT COUNT(*) FROM league_seasons ls
         WHERE ls.season_id = divisions.season_id) = 1;

-- 5. Registrations -> the LeagueSeason for their own per-Season league, else the
--    Season's sole LeagueSeason.
ALTER TABLE season_team_registrations ADD COLUMN league_season_id TEXT;
UPDATE season_team_registrations SET league_season_id = 'ls_' || league_id
  WHERE league_id IS NOT NULL
    AND EXISTS (SELECT 1 FROM league_seasons ls
                WHERE ls.id = 'ls_' || season_team_registrations.league_id);
UPDATE season_team_registrations SET league_season_id = (
  SELECT ls.id FROM league_seasons ls
  WHERE ls.season_id = season_team_registrations.season_id)
  WHERE league_season_id IS NULL
    AND (SELECT COUNT(*) FROM league_seasons ls
         WHERE ls.season_id = season_team_registrations.season_id) = 1;

-- 6. Teams gain a permanent League. Operator decisions are authoritative; every
--    other Team whose registrations resolve to exactly one League is assigned
--    it automatically. A Team with no registrations stays unassigned (nullable).
--    A decision may name any existing (per-Season) league; it is canonicalized
--    to that league's permanent id via its LeagueSeason row, so the Team is
--    never bound to a merged-away duplicate.
ALTER TABLE teams ADD COLUMN league_id TEXT;
UPDATE teams SET league_id = (
  SELECT ls.league_id FROM league_seasons ls
  JOIN team_league_migration_decisions d ON d.team_id = teams.id
  WHERE ls.id = 'ls_' || d.league_id)
  WHERE EXISTS (SELECT 1 FROM team_league_migration_decisions d
                WHERE d.team_id = teams.id);
UPDATE teams SET league_id = (
  SELECT MIN(ls.league_id) FROM season_team_registrations r
  JOIN league_seasons ls ON ls.id = r.league_season_id
  WHERE r.team_id = teams.id)
  WHERE league_id IS NULL
    AND (SELECT COUNT(DISTINCT ls.league_id)
         FROM season_team_registrations r
         JOIN league_seasons ls ON ls.id = r.league_season_id
         WHERE r.team_id = teams.id) = 1;

-- 7. Games keep their season_id/division_id but their league_id is repointed
--    from the deleted per-Season league to the merged permanent League. Teams,
--    scores, and standings are untouched.
UPDATE games SET league_id = (
  SELECT ls.league_id FROM league_seasons ls
  WHERE ls.id = 'ls_' || games.league_id)
  WHERE league_id IS NOT NULL
    AND EXISTS (SELECT 1 FROM league_seasons ls
                WHERE ls.id = 'ls_' || games.league_id);

-- 8. Drop the now-merged non-canonical league rows. league_seasons.league_id
--    holds only canonical ids, so any league not referenced there is a
--    duplicate whose divisions/registrations/games were reparented in 4-7.
DELETE FROM leagues WHERE id NOT IN (SELECT league_id FROM league_seasons);

-- 9. Retire the superseded columns now that every reference is reparented.
--    leagues: season_id -> program_id (index the new permanent parent).
ALTER TABLE leagues DROP COLUMN season_id;
CREATE INDEX IF NOT EXISTS ix_leagues_program ON leagues(program_id);

--    divisions: season_id + league_id -> league_season_id.
ALTER TABLE divisions DROP COLUMN season_id;
ALTER TABLE divisions DROP COLUMN league_id;
CREATE INDEX IF NOT EXISTS ix_divisions_league_season ON divisions(league_season_id);

--    registrations: drop both legacy indexes that pin season_id (the
--    UNIQUE(season_id, team_id) from #180 and the (season_id, division_id)
--    lookup), then season_id + league_id -> league_season_id. Uniqueness of
--    (team_id, league_season_id) is enforced (the gate proves no duplicate).
DROP INDEX IF EXISTS ux_season_team_registration;
DROP INDEX IF EXISTS ix_season_team_division;
ALTER TABLE season_team_registrations DROP COLUMN season_id;
ALTER TABLE season_team_registrations DROP COLUMN league_id;
CREATE UNIQUE INDEX IF NOT EXISTS ux_team_league_season
  ON season_team_registrations(team_id, league_season_id);
CREATE INDEX IF NOT EXISTS ix_reg_league_season_division
  ON season_team_registrations(league_season_id, division_id);

--    teams: index the new permanent League.
CREATE INDEX IF NOT EXISTS ix_teams_league ON teams(league_id);
