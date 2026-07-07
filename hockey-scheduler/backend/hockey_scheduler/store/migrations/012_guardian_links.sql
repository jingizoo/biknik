-- 012_guardian_links: guardian ↔ junior-player authorization links (#26).
--
-- Binds a guardian's login (guardian_user_id) to a junior player_id they may
-- respond for. `verified` gates real authority — an unverified link grants
-- nothing. No guardian contact/PII is stored here, only the two opaque ids, so
-- this table can never leak personal data into an operator view.

CREATE TABLE IF NOT EXISTS guardian_links (id TEXT PRIMARY KEY, guardian_user_id TEXT, player_id TEXT, created_at TEXT, verified INTEGER);

-- Look up all juniors for a guardian, and enforce one link per (guardian, junior).
CREATE INDEX IF NOT EXISTS ix_guardian_links_user ON guardian_links(guardian_user_id);
CREATE UNIQUE INDEX IF NOT EXISTS ix_guardian_links_pair ON guardian_links(guardian_user_id, player_id);
