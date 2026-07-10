-- 019_installation_state: durable one-time first-admin claim marker (#174 PR B).
--
-- Exactly one row (id = 'primary') records that the deployment has been claimed.
-- The primary key is the cross-process concurrency guard: two simultaneous claim
-- transactions cannot both insert it. No setup code or password is stored here.
-- Portable across SQLite and PostgreSQL: TEXT columns only.

CREATE TABLE IF NOT EXISTS installation_state (
  id TEXT PRIMARY KEY,
  claimed_at TEXT,
  claimed_by_user_id TEXT,
  claim_method TEXT
);
