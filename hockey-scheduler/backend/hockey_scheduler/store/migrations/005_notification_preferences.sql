-- 005_notification_preferences: per-recipient channel opt-outs (#81).
--
-- Gates whether an email/push delivery row is created when a notification is
-- enqueued. One row per (recipient_ref, channel); absent rows mean the channel
-- is on by default. The in-app feed is always delivered and is not stored here.

CREATE TABLE IF NOT EXISTS notification_preferences (
  id TEXT PRIMARY KEY,
  recipient_ref TEXT,
  channel TEXT,
  enabled INTEGER,
  digest TEXT
);

-- One preference per recipient/channel; also the lookup key on enqueue.
CREATE UNIQUE INDEX IF NOT EXISTS ix_notif_prefs_ref_channel
  ON notification_preferences(recipient_ref, channel);
