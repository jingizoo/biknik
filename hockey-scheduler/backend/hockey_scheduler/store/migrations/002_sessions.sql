-- 002_sessions: persistent, store-backed login sessions (#74).
--
-- Only the SHA-256 hash of a session token is stored; the raw token lives in
-- the client cookie. Role/scope are resolved from user_accounts on lookup, so
-- they are deliberately not columns here.

CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, token_hash TEXT, user_id TEXT, issued_at TEXT, expires_at TEXT, revoked_at TEXT, user_agent TEXT);

-- token_hash is the lookup key and must be unique; user_id backs revoke-by-user.
CREATE UNIQUE INDEX IF NOT EXISTS ix_sessions_token ON sessions(token_hash);
CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(user_id);
