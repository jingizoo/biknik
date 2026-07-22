-- One physical ice slot per (rink, start_time, end_time) — the atomic DB
-- backstop for the Ice Availability Builder's idempotent commit (#158 review).
--
-- Going forward this is a subset of the no-overlap invariant create_ice_slot
-- enforces in the service (two slots with the same rink+start+end necessarily
-- overlap). But the very TOCTOU this PR closes could already have LET a
-- concurrent writer persist an exact duplicate on a deployed database, so this
-- constraint is NOT free of existing violations: migration 045 registers the
-- assert_no_duplicate_ice_slot_times pre-migration check (see
-- _PRE_MIGRATION_CHECKS), which fails the upgrade and names the conflicting
-- (rink, start, end) tuple + slot ids before this DDL runs — never an opaque
-- CREATE UNIQUE INDEX driver error. Forward-only: once applied, concurrent
-- writers — two ice-availability commits, or a commit racing a manual create /
-- CSV import — can never land duplicate rows even if their pre-write
-- classification snapshots overlap; the race-losing INSERT fails and is
-- translated to a structured conflict (db_errors.translate_ice_slot_time_
-- conflict_exception), which the commit path treats as an idempotent duplicate.
CREATE UNIQUE INDEX IF NOT EXISTS ux_ice_slots_rink_time
    ON ice_slots (rink_id, start_time, end_time);
