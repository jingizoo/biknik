-- 033_factory_reset_challenge_lock: durable, cross-process challenge and
-- single-in-flight lock for the production factory reset (#256 review
-- blocker 5).
--
-- Both process-local (in one Python object's memory) in the first pass;
-- moved into the store so a preview() and a later execute() reaching
-- different server instances/processes still see the same outstanding
-- challenge, and "one reset in progress at a time" holds installation-wide
-- rather than only within one process.
--
-- Each table holds at most one row, identified by a fixed singleton id
-- ("singleton"). A new preview REPLACES any prior challenge (upsert); a
-- reset acquires the lock via a plain unique-PK INSERT — a second,
-- concurrent acquire attempt hits the same PRIMARY KEY and is translated to
-- IntegrityConflictError (#201 Slice 2) the same way
-- create_first_admin_if_unclaimed's installation-claim race already is.
--
-- Neither table is ever touched by clear_all_data() (#256): the lock row is
-- held for the duration of the wipe it guards, and the challenge row is
-- always consumed (deleted) before the wipe begins.
--
-- Portable across SQLite and PostgreSQL: TEXT columns only.

CREATE TABLE IF NOT EXISTS factory_reset_challenges (
  id TEXT PRIMARY KEY,
  token_hash TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  counts TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS factory_reset_locks (
  id TEXT PRIMARY KEY,
  actor_id TEXT NOT NULL,
  acquired_at TEXT NOT NULL
);
