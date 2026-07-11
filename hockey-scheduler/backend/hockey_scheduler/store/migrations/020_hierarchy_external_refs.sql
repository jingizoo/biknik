-- 020_hierarchy_external_refs: stable import-matching keys for the full setup
-- hierarchy (#174 PR E).
--
-- The client hierarchy import (organizations, leagues, venues, seasons, levels,
-- divisions) must be idempotent across repeat uploads — re-importing the same
-- spreadsheet updates the existing records instead of creating duplicates. Like
-- rinks/teams/players/officials before them (migrations 009-011), these
-- entities are matched by an external_ref column carrying the sheet's stable
-- code, never by name (which can legitimately change). Nullable so existing
-- rows upgrade safely; manually-created records simply have no code. Portable
-- across SQLite and PostgreSQL: TEXT columns only. The schema_migrations ledger
-- guards these non-idempotent ALTERs (see migrate()).

ALTER TABLE organizations ADD COLUMN external_ref TEXT;
ALTER TABLE leagues ADD COLUMN external_ref TEXT;
ALTER TABLE venues ADD COLUMN external_ref TEXT;
ALTER TABLE seasons ADD COLUMN external_ref TEXT;
ALTER TABLE levels ADD COLUMN external_ref TEXT;
ALTER TABLE divisions ADD COLUMN external_ref TEXT;

CREATE INDEX IF NOT EXISTS ix_organizations_external_ref ON organizations(external_ref);
CREATE INDEX IF NOT EXISTS ix_leagues_external_ref ON leagues(external_ref);
CREATE INDEX IF NOT EXISTS ix_venues_external_ref ON venues(external_ref);
CREATE INDEX IF NOT EXISTS ix_seasons_external_ref ON seasons(external_ref);
CREATE INDEX IF NOT EXISTS ix_levels_external_ref ON levels(external_ref);
CREATE INDEX IF NOT EXISTS ix_divisions_external_ref ON divisions(external_ref);
