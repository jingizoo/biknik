-- One physical ice slot per (rink, start_time, end_time) — the atomic DB
-- backstop for the Ice Availability Builder's idempotent commit (#158 review).
--
-- This is a strict subset of the no-overlap invariant create_ice_slot already
-- enforces in the service (two slots with the same rink+start+end necessarily
-- overlap), so no existing row can violate it. Promoting it to a unique index
-- means concurrent writers — two ice-availability commits, or a commit racing a
-- manual create / CSV import — can never land duplicate rows even if their
-- pre-write classification snapshots overlap: the race-losing INSERT fails and
-- is translated to a structured conflict (db_errors.translate_ice_slot_time_
-- conflict_exception), which the commit path treats as an idempotent duplicate.
CREATE UNIQUE INDEX IF NOT EXISTS ux_ice_slots_rink_time
    ON ice_slots (rink_id, start_time, end_time);
