"""Migration 035 competition-hierarchy reset (#283 Slice A).

Proves the forward-only Program -> League -> Team reset on seeded pre-035 data:
per-Season league rows are merged into permanent Leagues, divisions/
registrations/games are reparented, and each Team is assigned a permanent
League. The operator-decision gate halts on a Team registered across multiple
Leagues (the exception report) and applies once a decision is recorded — an
idempotent retry. Runs on SQLite and, when ``TEST_DATABASE_URL`` is set,
PostgreSQL.
"""

import os
import sys
import unittest

# Store-only test: add the backend to sys.path directly rather than importing
# ``helpers`` (which pulls in the services layer). Migration 035 is exercised
# through the store + integrity_checks alone.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hockey_scheduler.store.db import connect
from hockey_scheduler.store.sql_store import _apply_migration, _load_migrations
from hockey_scheduler.store.integrity_checks import (
    MigrationDataError, assert_competition_hierarchy_reset_ready,
    find_ambiguous_team_leagues, find_league_season_merge_collisions)

_RESET = "035_competition_hierarchy_reset"

_SEED = [
    "INSERT INTO programs(id,name) VALUES('prog_men','Adult Men')",
    "INSERT INTO seasons(id,program_id,name) VALUES('s_w','prog_men','Winter 2026')",
    "INSERT INTO seasons(id,program_id,name) VALUES('s_s','prog_men','Summer 2026')",
    # Per-Season Platinum + Bronze, sharing an external_ref across Seasons.
    "INSERT INTO leagues(id,season_id,name,sort_order,external_ref) VALUES('lg_plat_w','s_w','Platinum',0,'PLAT')",
    "INSERT INTO leagues(id,season_id,name,sort_order,external_ref) VALUES('lg_plat_s','s_s','Platinum',0,'PLAT')",
    "INSERT INTO leagues(id,season_id,name,sort_order,external_ref) VALUES('lg_bron_w','s_w','Bronze',0,'BRON')",
    "INSERT INTO leagues(id,season_id,name,sort_order,external_ref) VALUES('lg_bron_s','s_s','Bronze',0,'BRON')",
    "INSERT INTO divisions(id,season_id,name,age_group,league_id) VALUES('d_w','s_w','North','','lg_plat_w')",
    "INSERT INTO teams(id,name,program_id) VALUES('t_stable','Stable FC','prog_men')",
    "INSERT INTO teams(id,name,program_id) VALUES('t_amb','Ambiguous FC','prog_men')",
    # Stable team: Platinum both Seasons -> unambiguous.
    "INSERT INTO season_team_registrations(id,season_id,team_id,division_id,league_id,active) VALUES('r1','s_w','t_stable','d_w','lg_plat_w',1)",
    "INSERT INTO season_team_registrations(id,season_id,team_id,division_id,league_id,active) VALUES('r2','s_s','t_stable',NULL,'lg_plat_s',1)",
    # Ambiguous team: Platinum in Winter, Bronze in Summer -> two Leagues.
    "INSERT INTO season_team_registrations(id,season_id,team_id,division_id,league_id,active) VALUES('r3','s_w','t_amb',NULL,'lg_plat_w',1)",
    "INSERT INTO season_team_registrations(id,season_id,team_id,division_id,league_id,active) VALUES('r4','s_s','t_amb',NULL,'lg_bron_s',1)",
]


def _sql_backends():
    backends = [("sqlite", ":memory:")]
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        backends.append(("postgres", url))
    return backends


class CompetitionHierarchyResetMigrationTest(unittest.TestCase):
    def _fresh_through_034(self, backend, url):
        """A connection migrated up to (not including) 035, seeded with the
        pre-035 fixture. On PostgreSQL, drop the public schema first for
        isolation."""
        conn, dialect, _ = connect(url)
        cur = conn.cursor()
        if backend == "postgres":
            cur.execute("DROP SCHEMA public CASCADE")
            cur.execute("CREATE SCHEMA public")
        cur.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                    "(version TEXT PRIMARY KEY, applied_at TEXT)")
        for version, statements in _load_migrations(backend):
            if version == _RESET:
                break
            _apply_migration(conn, dialect, version, statements)
        for stmt in _SEED:
            cur.execute(stmt)
        if backend != "postgres":
            conn.commit()
        return conn, dialect, cur

    def _apply_reset(self, conn, dialect, backend):
        stmts = dict(_load_migrations(backend))[_RESET]
        _apply_migration(conn, dialect, _RESET, stmts)

    def test_gate_halts_then_applies_and_merges(self):
        for backend, url in _sql_backends():
            with self.subTest(backend=backend):
                conn, dialect, cur = self._fresh_through_034(backend, url)
                try:
                    # 1. The gate halts on the ambiguous team, naming it.
                    ambiguous = find_ambiguous_team_leagues(conn)
                    self.assertEqual([a["team_id"] for a in ambiguous], ["t_amb"])
                    self.assertEqual(find_league_season_merge_collisions(conn), [])
                    with self.assertRaises(MigrationDataError) as cm:
                        assert_competition_hierarchy_reset_ready(conn)
                    self.assertIn("t_amb", str(cm.exception))

                    # 2. Record an operator decision -> gate passes.
                    cur.execute(
                        "INSERT INTO team_league_migration_decisions"
                        "(id,team_id,league_id,note) VALUES"
                        "('dec1','t_amb','lg_plat_w','canonical Platinum')")
                    if backend != "postgres":
                        conn.commit()
                    assert_competition_hierarchy_reset_ready(conn)  # no raise

                    # 3. Apply the reset.
                    self._apply_reset(conn, dialect, backend)

                    # Canonical permanent leagues survive; duplicates deleted.
                    leagues = {r["id"]: r["program_id"] for r in
                               cur.execute("SELECT id, program_id FROM leagues")}
                    self.assertEqual(set(leagues), {"lg_bron_s", "lg_plat_s"})
                    self.assertTrue(all(p == "prog_men" for p in leagues.values()))

                    # One LeagueSeason per (canonical league, season).
                    ls = {(r["league_id"], r["season_id"]) for r in
                          cur.execute("SELECT league_id, season_id FROM league_seasons")}
                    self.assertEqual(ls, {
                        ("lg_plat_s", "s_w"), ("lg_plat_s", "s_s"),
                        ("lg_bron_s", "s_w"), ("lg_bron_s", "s_s")})

                    # Division + registrations reparented onto LeagueSeasons.
                    self.assertEqual(
                        cur.execute("SELECT league_season_id FROM divisions "
                                    "WHERE id='d_w'").fetchone()["league_season_id"],
                        "ls_lg_plat_w")
                    regs = {r["id"]: r["league_season_id"] for r in
                            cur.execute("SELECT id, league_season_id FROM "
                                        "season_team_registrations")}
                    self.assertEqual(regs, {
                        "r1": "ls_lg_plat_w", "r2": "ls_lg_plat_s",
                        "r3": "ls_lg_plat_w", "r4": "ls_lg_bron_s"})

                    # Teams: unambiguous auto-assigned; ambiguous uses the
                    # decision, CANONICALIZED to the surviving permanent league.
                    teams = {r["id"]: r["league_id"] for r in
                             cur.execute("SELECT id, league_id FROM teams")}
                    self.assertEqual(teams["t_stable"], "lg_plat_s")
                    self.assertEqual(teams["t_amb"], "lg_plat_s")
                    self.assertIn(teams["t_amb"], leagues)  # not dangling
                finally:
                    conn.close()

    def test_unambiguous_only_needs_no_decision(self):
        for backend, url in _sql_backends():
            with self.subTest(backend=backend):
                conn, dialect, cur = self._fresh_through_034(backend, url)
                try:
                    # Remove the ambiguous team's cross-league registration so
                    # every team resolves to a single league.
                    cur.execute("DELETE FROM season_team_registrations WHERE id='r4'")
                    if backend != "postgres":
                        conn.commit()
                    # Gate passes with no decision recorded.
                    assert_competition_hierarchy_reset_ready(conn)
                    self._apply_reset(conn, dialect, backend)
                    teams = {r["id"]: r["league_id"] for r in
                             cur.execute("SELECT id, league_id FROM teams")}
                    self.assertEqual(teams["t_stable"], "lg_plat_s")
                    self.assertEqual(teams["t_amb"], "lg_plat_s")
                finally:
                    conn.close()


if __name__ == "__main__":
    unittest.main()
