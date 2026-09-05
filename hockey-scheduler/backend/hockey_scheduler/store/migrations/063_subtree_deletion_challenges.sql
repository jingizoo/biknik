-- 063_subtree_deletion_challenges: durable, actor-scoped, single-use preview
-- challenges for explicit destructive subtree deletion (#429).  Raw bearer
-- tokens are never stored; only their SHA-256 digest is durable.
CREATE TABLE IF NOT EXISTS subtree_deletion_challenges (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    root_type TEXT NOT NULL,
    root_id TEXT NOT NULL,
    confirmation_name TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
