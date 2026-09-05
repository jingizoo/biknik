-- 064_cross_team_substitute_provenance: preserve both ends of a cross-team
-- substitute opt-in (#287).
--
-- team_id remains the durable TARGET game side.  source_membership_id and
-- source_team_id record the exact same-LeagueSeason membership that made a
-- cross-team opt-in eligible.  They deliberately carry no foreign keys:
-- substitute history must survive later roster-membership or organization
-- cleanup, just as cancellation snapshots survive facility deletion.
-- Existing/same-team enrollments remain NULL in both columns and keep their
-- established live-side retarget behaviour.

ALTER TABLE substitute_enrollments
    ADD COLUMN source_membership_id TEXT;

ALTER TABLE substitute_enrollments
    ADD COLUMN source_team_id TEXT;

-- A player may retain terminal substitute history for more than one side of
-- the same game.  Snapshot the side on each lifecycle event so a later side
-- switch cannot make that event ambiguous (or, worse, reattribute it to the
-- newest side).  These are historical facts, not live Team references: no
-- foreign keys and no backfill.  Legacy NULL rows keep the existing
-- conservative projection.
ALTER TABLE audit_logs
    ADD COLUMN team_id TEXT;

ALTER TABLE notification_events
    ADD COLUMN team_id TEXT;

-- One player can volunteer for only one side of a game at a time.  Terminal
-- rows remain as history and deliberately do not collide with a later
-- enrollment.  This also closes the cross-process race between two concurrent
-- Player Home checkbox requests for opposite sides of the same game.
CREATE UNIQUE INDEX IF NOT EXISTS ux_substitute_active_game_player
    ON substitute_enrollments (game_id, player_id)
    WHERE status IN ('enrolled', 'offered');
