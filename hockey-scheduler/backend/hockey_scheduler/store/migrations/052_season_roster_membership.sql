-- 052_season_roster_membership: seasonal athlete roster memberships on the
-- permanent Team + LeagueSeason spine (#205 Slice A).
--
-- (Numbered 052: 051 is reserved by the #273 athlete-identity slice, which
-- sits ahead of #205 in the owner-directed merge order on #212. The loader
-- applies versions in numeric order and tolerates gaps — 003 and 043 are
-- already absent.)
--
-- Today ``players.team_id`` makes Team membership PERMANENT: one player, one
-- team, forever, in every Season that team plays. #205 replaces that with a
-- season-scoped membership: a SeasonRosterMembership names one athlete's
-- participation stint for one Team in one Season, hanging off the Team's own
-- League's LeagueSeason (never a re-created League). Prior-Season stints are
-- separate immutable rows, so an athlete can represent Team A one Season and
-- Team B the next without any historical Game/roster/stat row changing.
--
-- Columns. ``player_id`` keys the membership to the EXISTING Player identity
-- — Player ids are preserved (the durable-athlete substrate is #273's, which
-- also preserves them), so this column plus the deterministic backfill ids
-- below IS the compatibility map between the legacy permanent model and the
-- seasonal one. ``season_id`` is carried alongside ``league_season_id``
-- (service-enforced consistent) so the authoritative-uniqueness index below
-- needs no join. ``position``/``jersey_number``/``shoots`` are the
-- season-scoped copies (#269's jersey integrity becomes
-- (LeagueSeason, Team)-scoped here); the permanent Player fields survive
-- unchanged until the consumer cutover. ``effective_from``/``effective_to``
-- bound the stint; NULL ``effective_from`` is first-class "predates
-- membership tracking" — the backfill never fabricates a date it cannot
-- know (same NULL-is-honest rule as migration 049).
--
-- Events. ``season_roster_membership_events`` is the append-only per-
-- membership change history the epic requires (status/jersey/position moves
-- with actor + reason + before/after detail). No store method updates or
-- deletes one. Backfilled memberships start with an EMPTY history: the
-- migration has no honest timestamp or actor to write, and inventing either
-- would be exactly the fabricated history #205 forbids.
--
-- Uniqueness (both engines support partial unique indexes, migration 022/
-- 029/038 precedent):
--   * one AUTHORITATIVE (status = 'active') membership per (player, Season).
--     Other statuses — including 'affiliate', the epic's governed call-up
--     exception — sit outside the rule on purpose. Terminal rows
--     ('released'/'transferred') accumulate as history and never collide.
--   * one active (jersey) per (league_season, team) among concrete numbers —
--     the season-Team-scoped successor of 038's per-team index, which stays
--     in place for the legacy Player fields until the consumer cutover.
--
-- Deterministic backfill (no guessing; pre-migration check
-- assert_season_roster_membership_backfill_ready aborts on anything else):
-- every ACTIVE player becomes an ACTIVE membership for each non-archived
-- Season their Team is actively registered in — the exact eligibility the
-- permanent model grants them today, no more and no less. Deliberately NOT
-- backfilled, and why:
--   * inactive players — a parked player holds no current participation;
--     minting them a membership row per Season would invent a stint nobody
--     recorded. Their history lives untouched in game_roster_entries.
--   * archived Seasons — retroactively writing memberships into read-only
--     history would fabricate it; historical Games keep resolving through
--     the untouched legacy fields until the consumer cutover defines
--     historical resolution.
-- Backfill ids are deterministic ('srm_legacy_' || player || '_' ||
-- league_season, migration 029's 'sva_legacy_' pattern), so the backfill is
-- reproducible, its provenance is self-evident, and a re-run collides on the
-- primary key instead of silently duplicating rows. GuardianLinks reference
-- players by id and are not touched: they stay valid because Player rows and
-- ids are not modified here at all.

CREATE TABLE IF NOT EXISTS season_roster_memberships (
    id TEXT PRIMARY KEY,
    player_id TEXT NOT NULL,
    league_season_id TEXT NOT NULL,
    season_id TEXT NOT NULL,
    team_id TEXT NOT NULL,
    status TEXT NOT NULL,
    position TEXT,
    jersey_number INTEGER,
    shoots TEXT,
    effective_from TEXT,
    effective_to TEXT,
    FOREIGN KEY (player_id) REFERENCES players(id),
    FOREIGN KEY (league_season_id) REFERENCES league_seasons(id),
    FOREIGN KEY (season_id) REFERENCES seasons(id),
    FOREIGN KEY (team_id) REFERENCES teams(id)
);

CREATE TABLE IF NOT EXISTS season_roster_membership_events (
    id TEXT PRIMARY KEY,
    membership_id TEXT NOT NULL,
    action TEXT NOT NULL,
    at TEXT NOT NULL,
    actor_id TEXT,
    reason TEXT,
    detail TEXT NOT NULL,
    FOREIGN KEY (membership_id) REFERENCES season_roster_memberships(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_srm_active_player_season
    ON season_roster_memberships (player_id, season_id)
    WHERE status = 'active';

CREATE UNIQUE INDEX IF NOT EXISTS ux_srm_active_team_jersey
    ON season_roster_memberships (league_season_id, team_id, jersey_number)
    WHERE status = 'active' AND jersey_number IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_srm_player
    ON season_roster_memberships (player_id);
CREATE INDEX IF NOT EXISTS ix_srm_league_season_team
    ON season_roster_memberships (league_season_id, team_id);
CREATE INDEX IF NOT EXISTS ix_srm_season
    ON season_roster_memberships (season_id);
CREATE INDEX IF NOT EXISTS ix_srme_membership
    ON season_roster_membership_events (membership_id);

INSERT INTO season_roster_memberships
    (id, player_id, league_season_id, season_id, team_id, status,
     position, jersey_number, shoots, effective_from, effective_to)
SELECT 'srm_legacy_' || p.id || '_' || ls.id,
       p.id, ls.id, ls.season_id, p.team_id, 'active',
       p.position, p.jersey_number, p.shoots, NULL, NULL
FROM players p
JOIN teams t ON t.id = p.team_id
JOIN season_team_registrations r ON r.team_id = t.id AND r.active = 1
JOIN league_seasons ls ON ls.id = r.league_season_id
JOIN seasons s ON s.id = ls.season_id AND s.status = 'active'
WHERE p.is_active = 1;
