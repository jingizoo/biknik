-- 034_team_league_migration_decisions: operator-decision table for the
-- competition-hierarchy reset (#283 Slice A).
--
-- Migration 035 assigns every Team a permanent League. A Team whose historical
-- registrations resolve to a single League migrates automatically; a Team with
-- registrations across MORE THAN ONE League is ambiguous and halts 035 with an
-- exception report until an operator records the intended permanent league_id
-- here. This table is created by its own migration (not 035) so it exists and
-- is writable while 035's pre-check keeps failing: an operator inserts the
-- missing decisions, and 035 re-runs and applies on the next startup (its
-- ledger row is only written once the pre-check passes, so the retry is safe).
CREATE TABLE IF NOT EXISTS team_league_migration_decisions (
  id TEXT PRIMARY KEY,
  team_id TEXT NOT NULL,
  league_id TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_team_league_migration_decision_team
  ON team_league_migration_decisions(team_id);
