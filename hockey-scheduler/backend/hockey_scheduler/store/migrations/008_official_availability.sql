-- 008_official_availability: officials declare available/unavailable windows (#88).
--
-- (Reserved number 008 was earmarked for scheduling constraints, which shipped
-- without schema in #85; reused here for the next table.) An unavailable window
-- overlapping a game warns/blocks assignment of that official.

CREATE TABLE IF NOT EXISTS official_availability (
  id TEXT PRIMARY KEY,
  official_id TEXT,
  start_time TEXT,
  end_time TEXT,
  status TEXT,
  note TEXT
);

CREATE INDEX IF NOT EXISTS ix_official_avail_official
  ON official_availability(official_id);
