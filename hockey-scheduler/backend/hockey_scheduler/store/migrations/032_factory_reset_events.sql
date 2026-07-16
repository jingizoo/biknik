-- 032_factory_reset_events: append-only record of production factory-reset
-- attempts (#256), stored outside the ordinary application audit trail.
--
-- The factory-reset operation (services/factory_reset_service.py) wipes every
-- ordinary table via store.clear_all_data() -- including setup_audit_logs
-- itself -- so no row written through the normal audit path can survive a
-- completed reset. This table is the one durable record of the attempt: it
-- is never touched by clear_all_data(), so it is the sole authoritative trail
-- an operator can inspect after a reset to confirm what happened and when.
--
-- No secrets or player PII: actor_id and environment are operational
-- identifiers, pre_reset_counts is a JSON map of table name -> row count
-- captured at preview time, and failure_reason (when result = "failed") is a
-- short, sanitized domain-error message, never a raw exception/traceback.
--
-- Portable across SQLite and PostgreSQL: TEXT/INTEGER columns only.

CREATE TABLE IF NOT EXISTS factory_reset_events (
  id TEXT PRIMARY KEY,
  actor_id TEXT NOT NULL,
  environment TEXT NOT NULL,
  started_at TEXT NOT NULL,
  result TEXT NOT NULL,
  pre_reset_counts TEXT,
  completed_at TEXT,
  failure_reason TEXT
);

CREATE INDEX IF NOT EXISTS ix_factory_reset_events_started_at
  ON factory_reset_events(started_at);
