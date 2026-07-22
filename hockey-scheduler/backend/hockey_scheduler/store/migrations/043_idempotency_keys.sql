-- Idempotency keys for externally-retried writes (#201).
--
-- A client that retries a POST sends the same Idempotency-Key header; the write
-- runs at most once per (actor, key) and every retry replays the recorded
-- response instead of creating a duplicate row. The record is written in the
-- SAME transaction as the write it dedups, and key_hash carries a UNIQUE index
-- so two concurrent retries race on that constraint (insert-first: one commits,
-- the loser rolls its duplicate back and replays) — the same cross-process
-- pattern the installation_state / factory_reset_lock singletons use.
--
-- Portable additive DDL (TEXT-only, no rebuild), so a single .sql covers both
-- SQLite and PostgreSQL. response holds the serialized result as JSON text.
CREATE TABLE IF NOT EXISTS idempotency_keys (
  id TEXT PRIMARY KEY,
  key_hash TEXT,
  actor_id TEXT,
  fingerprint TEXT,
  response TEXT,
  created_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_idempotency_keys_hash
  ON idempotency_keys (key_hash);
CREATE INDEX IF NOT EXISTS ix_idempotency_keys_created
  ON idempotency_keys (created_at);
