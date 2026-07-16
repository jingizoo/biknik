-- 030_contact_preference_lifecycle: a durable, reactivatable retired state
-- for contact destinations and notification preferences (#232 review 4).
--
-- Deleting either row outright let a stored opt-out disappear along with a
-- Player/Official whose delete was blocked by something else, or erased
-- integration history #232 requires be preserved. Only an ACTIVE row now
-- blocks delete_official/delete_player; retiring one (active=false) clears
-- the way without touching its stored destination/enabled value or its
-- history. DEFAULT 1 backfills every existing row as active, matching
-- today's behavior exactly (every row was implicitly "live").

ALTER TABLE contact_destinations ADD COLUMN active INTEGER DEFAULT 1;
ALTER TABLE notification_preferences ADD COLUMN active INTEGER DEFAULT 1;
