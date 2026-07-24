-- Scheduling policies for game placement (#277 Slice B).
--
-- One row per (scope_type, scope_id) — a Program, Season, or Rink — holding
-- the operational turnover/curfew knobs: warmup_minutes (pre-game ice
-- reservation), resurfacing_minutes (post-game resurfacing/bench turnover),
-- min_playable_minutes, and curfew_local ("HH:MM" wall clock in the venue's
-- timezone). Every column is nullable: NULL means "inherit from the next
-- scope up" (Rink > Season > Program, resolved field by field), and a field
-- unset at every scope is the no-op default — so this table starts empty and
-- existing installs keep today's behavior exactly (no data backfill, no time
-- shifts to any stored IceSlot/Game row).
--
-- The write path serializes on the SCOPE row's FOR UPDATE lock
-- (set_scheduling_policy), so concurrent upserts for one scope queue rather
-- than race; the unique index is the belt-and-braces backstop guaranteeing
-- duplicates can't exist even for a non-locking writer. No FK to the scope:
-- the scope column is polymorphic (program/season/rink), so scope deletes
-- instead cascade-delete the policy row in the service layer
-- (SetupService._cascade_scheduling_policy) — a row surviving its scope
-- would be permanently unreachable through the API, which validates scope
-- existence on every read/write. Portable additive DDL (TEXT/INTEGER only,
-- no rebuild).
CREATE TABLE IF NOT EXISTS scheduling_policies (
  id TEXT PRIMARY KEY,
  scope_type TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  warmup_minutes INTEGER,
  resurfacing_minutes INTEGER,
  min_playable_minutes INTEGER,
  curfew_local TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_scheduling_policies_scope
    ON scheduling_policies (scope_type, scope_id);
