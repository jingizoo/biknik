-- 021_permanent_teams: teams become permanent league members with
-- season-specific division registrations (#180).
--
-- Before this, a team belonged directly to a division, so it appeared to exist
-- only inside one season. Now Team.league_id makes the team a permanent member
-- of its league, and season_team_registrations carries the season-specific
-- division/group assignment (a team can be Division A one season and Division B
-- the next without changing the Team record or prior-season history). Unique
-- (season_id, team_id) enforces exactly one registration per team per season.
-- Portable across SQLite and PostgreSQL: TEXT/INTEGER columns and `||` concat
-- only. The schema_migrations ledger guards the non-idempotent ALTER/INSERT.

ALTER TABLE teams ADD COLUMN league_id TEXT;

CREATE TABLE IF NOT EXISTS season_team_registrations (
  id TEXT PRIMARY KEY,
  season_id TEXT NOT NULL,
  team_id TEXT NOT NULL,
  division_id TEXT,
  active INTEGER NOT NULL DEFAULT 1
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_season_team_registration
  ON season_team_registrations(season_id, team_id);
CREATE INDEX IF NOT EXISTS ix_teams_league ON teams(league_id);
CREATE INDEX IF NOT EXISTS ix_season_team_division
  ON season_team_registrations(season_id, division_id);

-- Deterministic backfill (no name guessing): a team's permanent league is its
-- current division's season league.
UPDATE teams SET league_id = (
  SELECT s.league_id FROM divisions d JOIN seasons s ON d.season_id = s.id
  WHERE d.id = teams.division_id
) WHERE league_id IS NULL AND division_id IS NOT NULL;

-- Create exactly one season registration per existing team-with-division,
-- preserving its current season/division placement. A fixed 'streg_legacy_'
-- prefix keeps these ids distinct from the runtime next_id('streg') counter, so
-- they can never collide. Teams whose division_id is dangling get no row and no
-- guessed league (surfaced as an orphan by the read model, not fabricated).
INSERT INTO season_team_registrations (id, season_id, team_id, division_id, active)
SELECT 'streg_legacy_' || t.id, d.season_id, t.id, t.division_id, 1
FROM teams t JOIN divisions d ON t.division_id = d.id;
