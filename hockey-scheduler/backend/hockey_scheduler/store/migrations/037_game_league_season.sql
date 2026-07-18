-- 037_game_league_season: a regular Game references its exact LeagueSeason
-- (#283 Slice E).
--
-- Until now a Game carried separate season_id + league_id values, which can
-- drift apart. A REGULAR game belongs to exactly one LeagueSeason (the
-- league_id × season_id join created in migration 035), and that pair is its
-- single source of competition identity. Exhibitions (game_type='exhibition',
-- #283 Slice D) have no owning LeagueSeason and keep it NULL.
--
-- Nullable column so exhibitions and any historical gap stay valid. The
-- backfill maps every REGULAR game with both a league and a season to the
-- unique LeagueSeason for that pair — a pure re-derivation that changes no
-- historical score, result, or standing. Portable across SQLite and
-- PostgreSQL (correlated-subquery UPDATE). The schema_migrations ledger guards
-- this non-idempotent ALTER (see migrate()).

ALTER TABLE games ADD COLUMN league_season_id TEXT;

UPDATE games
   SET league_season_id = (
       SELECT ls.id FROM league_seasons ls
        WHERE ls.league_id = games.league_id
          AND ls.season_id = games.season_id)
 WHERE game_type = 'regular'
   AND league_id IS NOT NULL
   AND season_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_games_league_season ON games(league_season_id);
