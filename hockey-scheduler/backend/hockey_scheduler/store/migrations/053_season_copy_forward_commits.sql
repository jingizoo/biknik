-- Idempotency ledger for new-Season copy-forward commits (#159 review round
-- 2): one committed Season per copy_forward_fingerprint -- the atomic DB
-- backstop closing the double-submit/retry blocker the owner ruled a real
-- defect, not an acceptable tradeoff.
--
-- commit_new_season_copy_forward's write target -- a brand-new Season -- has
-- no natural dedup key of its own (unlike commit_ice_availability's
-- (rink, start, end), migration 045): two Seasons created from identical
-- inputs are, by the domain model, two genuinely different rows. Before this
-- migration, resubmitting the SAME, still-valid fingerprint (a client retry,
-- a double-click, or two genuinely concurrent requests) passed every
-- preview-binding check again and minted a SECOND, distinct Season with
-- duplicate registrations.
--
-- This table records which Season a fingerprint actually produced, written
-- in the SAME transaction as the Season + registrations it describes.
-- ux_season_copy_forward_commits_fingerprint is the atomic backstop: a
-- second commit attempt -- sequential replay OR a genuine concurrent race,
-- two real connections both trying to INSERT the same fingerprint -- loses
-- the unique-index race. The loser's INSERT fails, its whole transaction
-- (including the Season + registrations it just built) rolls back, and the
-- service (db_errors.translate_copy_forward_commit_conflict_exception) reads
-- this row back and returns the WINNER's season/registrations/totals --
-- never a raw driver error, and never a second Season. The standard REST
-- idempotency-key replay, mirroring commit_ice_availability's own
-- idempotent-duplicate handling (#158) for a write target that has no
-- natural key to converge on.
--
-- registration_ids/rolled_forward/skipped are a snapshot of exactly what the
-- WINNING commit produced -- not re-derived from the Season's current
-- registrations -- so a later idempotent replay returns precisely the
-- original result even if an operator has since legitimately registered
-- more teams into the new Season by other means.
--
-- Portable across SQLite and PostgreSQL (plain DDL, no dialect-specific
-- syntax), so this ships as one file rather than a dialect pair. Brand new
-- table, always empty before this migration runs, so no pre-migration data
-- check is needed (contrast migration 045/048, which added a constraint to
-- an already-populated table).
CREATE TABLE season_copy_forward_commits (
    id TEXT PRIMARY KEY,
    copy_forward_fingerprint TEXT NOT NULL,
    season_id TEXT NOT NULL REFERENCES seasons(id),
    actor_id TEXT,
    registration_ids TEXT NOT NULL,
    rolled_forward INTEGER NOT NULL,
    skipped INTEGER NOT NULL,
    committed_at TEXT NOT NULL
);

CREATE UNIQUE INDEX ux_season_copy_forward_commits_fingerprint
    ON season_copy_forward_commits (copy_forward_fingerprint);
