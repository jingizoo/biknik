-- 006_calendar_feed_tokens: revocable bearer tokens for iCal feeds (#82).
--
-- Only the SHA-256 token_hash is stored; the raw token lives in the
-- subscription URL. One row per issued feed; possession scopes access to a
-- single actor (team / official / player).

CREATE TABLE IF NOT EXISTS calendar_feed_tokens (
  id TEXT PRIMARY KEY,
  token_hash TEXT,
  actor_type TEXT,
  actor_ref TEXT,
  created_at TEXT,
  revoked_at TEXT,
  label TEXT
);

-- token_hash is the lookup key on every feed request; actor index backs listing.
CREATE UNIQUE INDEX IF NOT EXISTS ix_calfeed_token ON calendar_feed_tokens(token_hash);
CREATE INDEX IF NOT EXISTS ix_calfeed_actor ON calendar_feed_tokens(actor_type, actor_ref);
