-- Require a game for every result + finalize the uniqueness index (#201 Slice 3E).
--
-- Completes the result → game relationship that Slice 3D (#229) started: 3D added
-- the foreign key but left game_id nullable and deferred requiring it. Now every
-- result must name a game.
--
-- With game_id NOT NULL, the Slice 3C partial predicate (WHERE game_id IS NOT
-- NULL) is vacuous and the separate non-unique lookup index is redundant — a
-- plain UNIQUE index on game_id both enforces one-result-per-game and serves the
-- per-game read (result_for_game, WHERE game_id = ?). So the partial unique and
-- the lookup index are replaced by a single plain unique index.
--
-- A forward-only pre-migration check (store.integrity_checks) reports any result
-- row with a NULL game_id before the column is required, so an upgrade against
-- such rows fails loudly, naming them, rather than with an opaque ALTER error.
ALTER TABLE game_results ALTER COLUMN game_id SET NOT NULL;
DROP INDEX IF EXISTS ux_game_result_game;
DROP INDEX IF EXISTS ix_game_results_game;
CREATE UNIQUE INDEX ux_game_result_game ON game_results (game_id);
