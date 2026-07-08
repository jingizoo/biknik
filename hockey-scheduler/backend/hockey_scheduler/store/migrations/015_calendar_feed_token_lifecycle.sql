-- 015_calendar_feed_token_lifecycle: who minted/revoked a feed token and
-- when it was last actually fetched (#131).
--
-- created_by/revoked_by are a signed-in user id, or the literal string
-- "anonymous" for a public team/division feed minted with no session (#33).
-- last_used_at is bumped on every successful .ics fetch. All nullable:
-- existing tokens simply have no recorded history for these columns yet.

ALTER TABLE calendar_feed_tokens ADD COLUMN created_by TEXT;
ALTER TABLE calendar_feed_tokens ADD COLUMN last_used_at TEXT;
ALTER TABLE calendar_feed_tokens ADD COLUMN revoked_by TEXT;
