-- 018_league_owner_and_venue: tie leagues to a facility owner and venues to a
-- single league (#173).
--
-- League.organization_id links a league up to the Organization that operates
-- it. Venue.league_id links a venue to at most one league (a scalar column, so
-- one venue can never hold two leagues — the client's hard rule). Both are
-- nullable so existing rows upgrade safely; the missing-assignment queue
-- surfaces null/mismatched rows until an operator fixes them. Portable across
-- SQLite and PostgreSQL: TEXT columns only. The schema_migrations ledger guards
-- the non-idempotent ALTERs (see migrate()).

ALTER TABLE leagues ADD COLUMN organization_id TEXT;
ALTER TABLE venues ADD COLUMN league_id TEXT;

CREATE INDEX IF NOT EXISTS ix_leagues_organization ON leagues(organization_id);
CREATE INDEX IF NOT EXISTS ix_venues_league ON venues(league_id);
