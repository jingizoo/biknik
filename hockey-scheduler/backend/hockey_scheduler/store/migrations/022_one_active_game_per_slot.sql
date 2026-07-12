-- One active (non-cancelled) game may occupy an ice slot at a time (#201 Slice 3A).
--
-- A partial UNIQUE index enforces the invariant the service layer already checks
-- (game_using_ice_slot ignores cancelled games), so it holds even under a
-- cross-process race that application-level checks cannot serialize. The rule for
-- inactive games is explicit: a cancelled game keeps its historical ice_slot_id
-- for the record but does NOT reserve the slot (it is excluded here), and a
-- slot-less game (ice_slot_id IS NULL) is not constrained.
--
-- Portable across SQLite and PostgreSQL: both support partial unique indexes,
-- and `cancelled` is an INTEGER 0/1 column in both dialects.
--
-- A forward-only pre-migration check (store.integrity_checks) reports any
-- pre-existing duplicate active assignments before this index is created, so an
-- upgrade fails loudly with the offending slots rather than an opaque index error.
CREATE UNIQUE INDEX IF NOT EXISTS ux_games_active_ice_slot
  ON games (ice_slot_id) WHERE cancelled = 0 AND ice_slot_id IS NOT NULL;
