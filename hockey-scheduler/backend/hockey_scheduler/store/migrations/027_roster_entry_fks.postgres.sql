-- Referential integrity: a roster entry references a real game and player
-- (#201 Slice 3F).
--
-- game_roster_entries.game_id → games(id) and .player_id → players(id). Both
-- columns stay NULLABLE (a nullable foreign key still permits NULL); only a
-- concrete game_id/player_id must now name a row that exists, so a stray
-- reference can no longer be written. Requiring the columns (NOT NULL) and
-- collapsing the Slice 3B indexes is a later slice, mirroring how the result
-- table was completed (3D → 3E).
--
-- PostgreSQL enforces declared foreign keys natively, so two ADD CONSTRAINTs are
-- enough. (The SQLite variant rebuilds the table because SQLite cannot add a
-- foreign key to an existing table.)
--
-- A forward-only pre-migration check (store.integrity_checks) reports any roster
-- row whose non-null game_id or player_id names a missing parent before the
-- constraints are added, so an upgrade against dangling data fails loudly,
-- naming the rows, rather than with an opaque constraint error.
ALTER TABLE game_roster_entries
  ADD CONSTRAINT fk_roster_game
  FOREIGN KEY (game_id) REFERENCES games (id);
ALTER TABLE game_roster_entries
  ADD CONSTRAINT fk_roster_player
  FOREIGN KEY (player_id) REFERENCES players (id);
