-- Cancellation releases physical ice while preserving display-stable history
-- on the Game (#428).  Snapshot ids deliberately carry no foreign keys: #429
-- may later delete the original facility subtree, but a cancelled fixture must
-- continue to name the venue, rink and times at which it had been scheduled.
ALTER TABLE games ADD COLUMN cancelled_ice_slot_id TEXT;
ALTER TABLE games ADD COLUMN cancelled_venue_id TEXT;
ALTER TABLE games ADD COLUMN cancelled_venue_name TEXT;
ALTER TABLE games ADD COLUMN cancelled_venue_timezone TEXT;
ALTER TABLE games ADD COLUMN cancelled_rink_id TEXT;
ALTER TABLE games ADD COLUMN cancelled_rink_name TEXT;
ALTER TABLE games ADD COLUMN cancelled_scheduled_start_time TEXT;
ALTER TABLE games ADD COLUMN cancelled_scheduled_end_time TEXT;
ALTER TABLE games ADD COLUMN cancelled_ice_start_time TEXT;
ALTER TABLE games ADD COLUMN cancelled_ice_end_time TEXT;

-- Repair already-cancelled rows written by the old behavior.  Correlated
-- scalar subqueries keep this migration portable across SQLite/PostgreSQL.
-- The EXISTS clause means a corrupt dangling reference is never detached
-- without a complete history snapshot.
UPDATE games
SET cancelled_ice_slot_id = ice_slot_id,
    cancelled_venue_id = (
        SELECT v.id FROM ice_slots s
        JOIN rinks r ON r.id = s.rink_id
        JOIN venues v ON v.id = r.venue_id
        WHERE s.id = games.ice_slot_id),
    cancelled_venue_name = (
        SELECT v.name FROM ice_slots s
        JOIN rinks r ON r.id = s.rink_id
        JOIN venues v ON v.id = r.venue_id
        WHERE s.id = games.ice_slot_id),
    cancelled_venue_timezone = (
        SELECT v.timezone FROM ice_slots s
        JOIN rinks r ON r.id = s.rink_id
        JOIN venues v ON v.id = r.venue_id
        WHERE s.id = games.ice_slot_id),
    cancelled_rink_id = (
        SELECT r.id FROM ice_slots s
        JOIN rinks r ON r.id = s.rink_id
        WHERE s.id = games.ice_slot_id),
    cancelled_rink_name = (
        SELECT r.name FROM ice_slots s
        JOIN rinks r ON r.id = s.rink_id
        WHERE s.id = games.ice_slot_id),
    cancelled_scheduled_start_time = start_time,
    cancelled_scheduled_end_time = end_time,
    cancelled_ice_start_time = (
        SELECT s.start_time FROM ice_slots s
        WHERE s.id = games.ice_slot_id),
    cancelled_ice_end_time = (
        SELECT s.end_time FROM ice_slots s
        WHERE s.id = games.ice_slot_id)
WHERE cancelled = 1
  AND ice_slot_id IS NOT NULL
  AND cancelled_ice_slot_id IS NULL
  AND EXISTS (
      SELECT 1 FROM ice_slots s
      JOIN rinks r ON r.id = s.rink_id
      JOIN venues v ON v.id = r.venue_id
      WHERE s.id = games.ice_slot_id);

-- Detach only rows whose snapshot completed.  A released slot becomes
-- AVAILABLE only when no other active Game currently occupies it (legacy
-- databases could already contain a cancelled row plus a replacement Game).
UPDATE games
SET ice_slot_id = NULL
WHERE cancelled = 1
  AND cancelled_ice_slot_id IS NOT NULL;

UPDATE ice_slots
SET status = 'available'
WHERE status = 'allocated'
  AND id IN (
      SELECT cancelled_ice_slot_id FROM games
      WHERE cancelled = 1 AND cancelled_ice_slot_id IS NOT NULL)
  AND NOT EXISTS (
      SELECT 1 FROM games
      WHERE games.ice_slot_id = ice_slots.id AND games.cancelled = 0);
