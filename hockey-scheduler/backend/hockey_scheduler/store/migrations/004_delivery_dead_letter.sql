-- 004_delivery_dead_letter: dead-letter operations for notification deliveries (#80).
--
-- Adds retry-scheduling / dead-letter timestamps to notification_deliveries.
-- (003 is intentionally unused — the session-cleanup slice #77 needed no
-- schema change.) One ADD COLUMN per statement so this runs on SQLite too;
-- the migration ledger applies it exactly once, so no IF NOT EXISTS is needed.

ALTER TABLE notification_deliveries ADD COLUMN last_attempt_at TEXT;
ALTER TABLE notification_deliveries ADD COLUMN next_attempt_at TEXT;
ALTER TABLE notification_deliveries ADD COLUMN dead_lettered_at TEXT;
