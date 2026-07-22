-- Per-user active Program/Season context (#159).
--
-- The selection the shell's switcher persists so every screen resolves to the
-- same Program+Season and deep links can restore it. One row per user (id = the
-- user_id). A view preference only — it grants no authority (roles/scope are
-- enforced independently, #211) — and a program_id/season_id that has since been
-- deleted or archived is ignored at resolve time in favor of a deterministic
-- authorized fallback. Portable additive DDL (TEXT-only, no rebuild).
CREATE TABLE IF NOT EXISTS user_active_context (
  id TEXT PRIMARY KEY,
  program_id TEXT,
  season_id TEXT,
  updated_at TEXT
);
