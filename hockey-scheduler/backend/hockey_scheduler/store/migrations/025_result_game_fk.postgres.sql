-- Referential integrity: a result must reference a real game (#201 Slice 3D).
--
-- game_results.game_id → games(id). The column stays NULLABLE (a nullable
-- foreign key still permits NULL — a result not yet tied to a game); only a
-- concrete game_id must now name a game that exists, so a stray reference can no
-- longer be written. Requiring the column (NOT NULL) is a later slice.
--
-- PostgreSQL enforces declared foreign keys natively, so a single ADD CONSTRAINT
-- is enough. (The SQLite variant rebuilds the table because SQLite cannot add a
-- foreign key to an existing table.)
--
-- A forward-only pre-migration check (store.integrity_checks) reports any result
-- row whose non-null game_id names a missing game before the constraint is
-- added, so an upgrade against dangling data fails loudly, naming the rows,
-- rather than with an opaque constraint error.
ALTER TABLE game_results
  ADD CONSTRAINT fk_game_results_game
  FOREIGN KEY (game_id) REFERENCES games (id);
