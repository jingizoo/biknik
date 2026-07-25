-- One Official per external_ref, and one availability window per
-- (official, start, end) -- the atomic DB backstop for the officials +
-- availability CSV import commit (#331 review round 11 finding 2).
--
-- commit_officials_availability_import has no row to lock for a brand-new
-- official_code, unlike commit_teams_players_import's Season lock (which
-- serializes its whole write set): officials aren't season-scoped, so
-- there is no pre-existing parent row two concurrent commits could both
-- take a FOR UPDATE lock on before checking absence. Two transactions can
-- each see an official_code (or an (official_id, start, end) window)
-- absent and both create their own row.
--
-- Both columns are nullable (an official_code is optional; a hand-entered
-- availability row could in principle omit a field), and both SQLite and
-- PostgreSQL treat NULLs as distinct in a unique index, so the partial
-- predicates below make explicit exactly what each index enforces --
-- mirroring migration 023's identical reasoning for game_roster_entries.
--
-- Forward-only, so existing data may already violate these: registers
-- assert_officials_availability_import_constraints_ready (see
-- _PRE_MIGRATION_CHECKS), which names the conflicting external_ref or
-- tuple + row ids before this DDL runs, rather than an opaque index error.
-- Once applied, a race-losing INSERT is translated to the same stable
-- IntegrityConflictError migration 045 already established
-- (db_errors.translate_db_exception's generic unique_violation fallback),
-- which commit_officials_availability_import retries as a whole batch --
-- mirroring commit_ice_availability's identical retry loop -- converging
-- once the winning transaction's row is visible to the fresh absence
-- checks on the next attempt.
CREATE UNIQUE INDEX IF NOT EXISTS ux_officials_external_ref
    ON officials (external_ref)
    WHERE external_ref IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_official_availability_official_time
    ON official_availability (official_id, start_time, end_time)
    WHERE official_id IS NOT NULL AND start_time IS NOT NULL
        AND end_time IS NOT NULL;
