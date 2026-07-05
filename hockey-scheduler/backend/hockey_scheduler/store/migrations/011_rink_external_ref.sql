-- 011_rink_external_ref: idempotent CSV-import matching key (#95).
--
-- Rinks imported from a spreadsheet are matched across repeat uploads by
-- this code (the rink_code column), not by name (which can legitimately
-- change).
ALTER TABLE rinks ADD COLUMN external_ref TEXT;
