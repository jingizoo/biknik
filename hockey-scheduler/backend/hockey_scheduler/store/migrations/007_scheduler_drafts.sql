-- 007_scheduler_drafts: mark scheduler-generated draft games (#86).
--
-- A draft game is invisible to the public schedule until published (which
-- clears the flag). Additive column; applied once via the ledger.

ALTER TABLE games ADD COLUMN is_draft INTEGER;
