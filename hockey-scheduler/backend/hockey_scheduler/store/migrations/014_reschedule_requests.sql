-- 014_reschedule_requests: reschedule request/approval workflow (#29).
--
-- A controlled request -> opponent response -> league decision flow for
-- moving a PUBLISHED game, distinct from the operator-only move_game
-- drag/drop. `status` drives the state machine; `new_ice_slot_id` and
-- `decision_note` are populated once the league decides.

CREATE TABLE IF NOT EXISTS reschedule_requests (
    id TEXT PRIMARY KEY,
    game_id TEXT,
    requested_by_team_id TEXT,
    reason TEXT,
    status TEXT,
    created_at TEXT,
    opponent_responded_at TEXT,
    league_decided_at TEXT,
    new_ice_slot_id TEXT,
    decision_note TEXT
);

CREATE INDEX IF NOT EXISTS ix_reschedule_requests_game ON reschedule_requests(game_id);
