-- Ice turnover / warm-up / resurfacing / curfew policy (#277).
--
-- Adds the reserved-vs-playable buffers to ice_slots and the ice-operations
-- policy fields to rinks and programs. The IceSlot buffers are NOT NULL DEFAULT
-- 0 so every existing slot's playable window equals its reserved window — no
-- existing schedule time-shifts. The rink/program policy fields are nullable:
-- NULL means "inherit" (Rink value, else Program value, else the system default
-- of turnover 0 / no curfew / buffers 0), resolved by
-- services/ice_policy.effective_policy — never written back as a concrete value.
--
-- Portable additive DDL — one ADD COLUMN per statement so SQLite (which allows a
-- single column per ALTER) and PostgreSQL both apply it; no table rebuild, no
-- data rewrite. curfew_local is an "HH:MM" wall-clock end-by in the Program tz.

ALTER TABLE ice_slots ADD COLUMN warmup_minutes INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ice_slots ADD COLUMN resurface_minutes INTEGER NOT NULL DEFAULT 0;

ALTER TABLE rinks ADD COLUMN turnover_minutes INTEGER;
ALTER TABLE rinks ADD COLUMN curfew_local TEXT;
ALTER TABLE rinks ADD COLUMN warmup_minutes INTEGER;
ALTER TABLE rinks ADD COLUMN resurface_minutes INTEGER;

ALTER TABLE programs ADD COLUMN turnover_minutes INTEGER;
ALTER TABLE programs ADD COLUMN curfew_local TEXT;
ALTER TABLE programs ADD COLUMN warmup_minutes INTEGER;
ALTER TABLE programs ADD COLUMN resurface_minutes INTEGER;
