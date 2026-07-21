-- Referential integrity for reassignment writes: a player belongs to a real
-- team, and a team belongs to a real club (#201 Slice 2).
--
-- players.team_id → teams(id) and teams.club_id → clubs(id). These close the
-- reviewer-flagged reassignment races handed to #201: assign_player_team vs
-- delete_team, assign_team_club vs delete_club, and add_player onto a team being
-- deleted. delete_team/delete_club already block on dependents at the service
-- layer, but under PostgreSQL READ COMMITTED a writer that read its parent as
-- present can still commit after a concurrent delete removed it; the foreign key
-- is the database backstop that rejects that orphaning write regardless of
-- ordering, so no player is ever stranded on a deleted team and no team on a
-- deleted club.
--
-- Both columns stay NULLABLE (a nullable foreign key still permits NULL); only a
-- concrete id must now name a row that exists. teams.club_id is legitimately
-- optional (#233 Slice D), and requiring players.team_id (NOT NULL) is out of
-- scope here. A racing write's constraint violation is translated at the store
-- boundary into a stable, secret-free domain conflict (team_not_found /
-- club_not_found), not a raw driver error.
--
-- PostgreSQL enforces declared foreign keys natively, so two ADD CONSTRAINTs are
-- enough. (The SQLite variant rebuilds both tables because SQLite cannot add a
-- foreign key to an existing table.)
--
-- A forward-only pre-migration check (store.integrity_checks:
-- assert_reassignment_fks_ready) reports any player whose non-null team_id names
-- a missing team and any team whose non-null club_id names a missing club before
-- the constraints are added, so an upgrade against dangling data fails loudly,
-- naming the rows, rather than with an opaque constraint error.
-- ON DELETE NO ACTION is spelled out (not left to PostgreSQL's default) so the
-- restrictive, non-cascading safety contract is visible in the DDL: deleting a
-- still-referenced team or club is rejected, never cascaded.
ALTER TABLE teams
  ADD CONSTRAINT fk_teams_club
  FOREIGN KEY (club_id) REFERENCES clubs (id) ON DELETE NO ACTION;
ALTER TABLE players
  ADD CONSTRAINT fk_players_team
  FOREIGN KEY (team_id) REFERENCES teams (id) ON DELETE NO ACTION;
