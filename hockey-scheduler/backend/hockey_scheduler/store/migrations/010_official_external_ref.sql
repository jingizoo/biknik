-- 010_official_external_ref: idempotent CSV-import matching key (#94).
--
-- Officials imported from a spreadsheet are matched across repeat uploads
-- by this code (the official_code column), not by name.
ALTER TABLE officials ADD COLUMN external_ref TEXT;
