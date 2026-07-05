-- 009_team_player_external_ref: idempotent CSV-import matching key (#93).
--
-- Teams and players imported from a spreadsheet are matched across repeat
-- uploads by this code, not by name (which can legitimately change).
ALTER TABLE teams ADD COLUMN external_ref TEXT;
ALTER TABLE players ADD COLUMN external_ref TEXT;
