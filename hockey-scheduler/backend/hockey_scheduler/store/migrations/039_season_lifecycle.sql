-- 039_season_lifecycle: Season archive/read-only lifecycle (#159).
--
-- Seasons gain an explicit lifecycle state. Existing rows are ACTIVE (the
-- prior implicit behaviour), so operational writes are unaffected until an
-- authorized, reasoned archive flips a Season to read-only historical mode.
-- `status` is a lowercase enum string ('active' | 'archived'); `archived_at`
-- stamps when it was archived (NULL while active). Portable across SQLite and
-- Postgres; the schema_migrations ledger applies it exactly once.
ALTER TABLE seasons ADD COLUMN status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE seasons ADD COLUMN archived_at TEXT;
