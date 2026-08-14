-- 053_data_access_log: durable sensitive-read audit rows (#124).
--
-- NUMBERING NOTE (parallel lanes): versions 051 and 052 are already claimed
-- by in-flight branches (#273 athlete identity, #205 SeasonRosterMembership).
-- This slice takes 053; if those branches land in a different order the
-- renumbering happens at merge-time rebase, per the queue agreement.
--
-- One row per sensitive read — or refused read attempt — of one subject's
-- protected data (domain/privacy.py: DataAccessLog). Deliberately SEPARATE
-- from audit_logs/setup_audit_logs: read auditing has its own retention and
-- export story (#124 blocks 3-4, out of this slice), and mixing it into the
-- mutation trail would weld those decisions together.
--
-- STRUCTURALLY VALUE-FREE: there is no column for the value that was read,
-- and no free-form JSON column a value could be smuggled into. Recording
-- that a contact destination or birthdate was read must never copy it here.
--
-- Portable additive DDL (TEXT only, CREATE IF NOT EXISTS, no rebuild, no
-- backfill): the table starts empty everywhere and existing rows are never
-- touched, so this is safe on a populated upgrade in either engine.
CREATE TABLE IF NOT EXISTS data_access_logs (
    id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    purpose TEXT NOT NULL,
    at TEXT NOT NULL,
    actor_user_id TEXT,
    actor_role TEXT,
    outcome TEXT NOT NULL,
    request_id TEXT
);

-- The two queries the log exists to answer: "every access to THIS subject's
-- data" (export/data-rights, #124 block 3) and "everything THIS actor read"
-- (accountability review).
CREATE INDEX IF NOT EXISTS ix_data_access_subject
    ON data_access_logs (subject_type, subject_id);
CREATE INDEX IF NOT EXISTS ix_data_access_actor
    ON data_access_logs (actor_user_id);
