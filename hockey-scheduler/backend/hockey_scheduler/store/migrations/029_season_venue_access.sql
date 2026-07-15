-- 029_season_venue_access: decouple Venues from Programs via an explicit
-- many-to-many join (#233 Slice E, E1).
--
-- Physical structure (Facility Organization -> Venue -> Rink -> Ice Slot) and
-- competition structure (Program -> Season -> League -> optional Division)
-- are independent trees. The legacy venues.league_id (a Program id)
-- permanently ties one Venue to one Program, which cannot express "one
-- Season uses several Venues" or "one Venue hosts several Programs/Seasons".
-- This table is the explicit join, scoped to Season (not Program), so both
-- become possible. venues.league_id itself is untouched here — Slice E2
-- removes the bridge once the frontend/game-eligibility cutover lands.
--
-- A partial UNIQUE index enforces one ACTIVE row per (season_id, venue_id)
-- pair, mirroring migration 022's ux_games_active_ice_slot pattern: revoking
-- access deactivates the row (history preserved) rather than deleting it,
-- and re-granting reactivates the existing row rather than duplicating it —
-- the service layer enforces the reactivate-in-place rule (the same pattern
-- already used by register_team_for_season), and this index makes the
-- invariant hold even under a cross-process race.
--
-- Portable across SQLite and PostgreSQL: TEXT/INTEGER columns, a partial
-- unique index (both dialects support it), and `||` concat only.
--
-- Deterministic backfill (no name guessing): a legacy Venue.league_id link is
-- migrated to a SeasonVenueAccess row only when the target Program has
-- EXACTLY ONE Season — the sole Season a venue-wide link could unambiguously
-- have meant. A Program with zero Seasons has nothing to backfill. A Program
-- with two or more Seasons is ambiguous (which Season(s) should get access?)
-- and is never guessed here — the pre-migration check
-- assert_venue_season_access_backfill_ready (store.integrity_checks) aborts
-- the whole upgrade first, naming every such venue/program, so an operator
-- resolves it (or grants access explicitly once the Slice E2 UI lands)
-- rather than silently getting a partial or wrong backfill.

CREATE TABLE IF NOT EXISTS season_venue_access (
  id TEXT PRIMARY KEY,
  season_id TEXT NOT NULL,
  venue_id TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_season_venue_access_active
  ON season_venue_access(season_id, venue_id) WHERE active = 1;
CREATE INDEX IF NOT EXISTS ix_season_venue_access_season
  ON season_venue_access(season_id);
CREATE INDEX IF NOT EXISTS ix_season_venue_access_venue
  ON season_venue_access(venue_id);

INSERT INTO season_venue_access (id, season_id, venue_id, active)
SELECT 'sva_legacy_' || v.id, s.id, v.id, 1
FROM venues v
JOIN seasons s ON s.program_id = v.league_id
WHERE v.league_id IS NOT NULL
  AND (SELECT COUNT(*) FROM seasons s2 WHERE s2.program_id = v.league_id) = 1;
