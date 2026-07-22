-- Per-user active Program/Season context (#159).
--
-- The user's selected Program+Season, persisted one row per user (id = user_id;
-- season_id nullable for a Program-only context). A view preference only — it
-- grants no authority (roles/scope are enforced independently, #211). On resolve
-- the saved selection is filtered through the caller's authorized scope: an
-- archived Season is honored as a read-only historical context, while a deleted
-- or no-longer-authorized one is ignored in favor of a deterministic authorized
-- fallback (the row is not rewritten). set_active_context upserts atomically
-- (INSERT .. ON CONFLICT (id) DO UPDATE), last-write-wins. Portable additive DDL
-- (TEXT-only, no rebuild).
CREATE TABLE IF NOT EXISTS user_active_context (
  id TEXT PRIMARY KEY,
  program_id TEXT,
  season_id TEXT,
  updated_at TEXT
);
