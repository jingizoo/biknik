-- 031_club_external_ref: stable import-matching key for Club (#260 Slice F).
--
-- Club was the one setup entity left matched by name only (#214 review,
-- carried forward through migration 020): it had no stable external code, so
-- the hierarchy importer deliberately deferred Club identity rather than risk
-- merging two same-named Clubs. This closes that gap the same way rinks/
-- teams/players/officials/organizations/leagues/venues/seasons/levels/
-- divisions already were (migrations 009-011, 020): an external_ref column
-- carrying the sheet's stable club_code, never the display name. Nullable so
-- existing rows upgrade safely; manually-created Clubs simply have no code.
-- Portable across SQLite and PostgreSQL: TEXT column only. The
-- schema_migrations ledger guards this non-idempotent ALTER (see migrate()).

ALTER TABLE clubs ADD COLUMN external_ref TEXT;

CREATE INDEX IF NOT EXISTS ix_clubs_external_ref ON clubs(external_ref);
