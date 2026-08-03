-- 050_schedule_scenarios: immutable named schedule-generation evidence (#378).
--
-- Scenarios contain a reviewed proposal and the authoritative material-input
-- snapshot that produced it.  They do not own Games: committing creates the
-- existing unpublished draft Games and publishing remains a separate action.

CREATE TABLE IF NOT EXISTS schedule_scenarios (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    program_id TEXT NOT NULL,
    season_id TEXT NOT NULL,
    league_id TEXT NOT NULL,
    league_season_id TEXT NOT NULL,
    division_id TEXT,
    planner_version TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    proposal_fingerprint TEXT NOT NULL,
    request_input TEXT NOT NULL,
    proposal TEXT NOT NULL,
    generation_snapshot TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT,
    FOREIGN KEY (program_id) REFERENCES programs(id),
    FOREIGN KEY (season_id) REFERENCES seasons(id),
    FOREIGN KEY (league_id) REFERENCES leagues(id),
    FOREIGN KEY (league_season_id) REFERENCES league_seasons(id),
    FOREIGN KEY (division_id) REFERENCES divisions(id)
);

CREATE INDEX IF NOT EXISTS ix_schedule_scenarios_scope
    ON schedule_scenarios(program_id, season_id, league_id, division_id);
