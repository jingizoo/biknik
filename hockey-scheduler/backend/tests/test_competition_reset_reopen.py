"""Reopen proof for a database upgraded through migration 028 (#233 Slice C1b).

The competition-model reset is a forward-only, id-stable in-place migration. This
proves the durability promise for an EXISTING (pre-028) installation: build the
pre-028 schema with representative rows, migrate 028 forward, CLOSE the store,
REOPEN a fresh store against the same database, and read every canonical entity
back — Program / Season / League / Division / Team / SeasonTeamRegistration /
Game — asserting ids and relationships survive the restart.

Modeled on test_production_restart.py's close/reopen pattern and
test_competition_reset_migration.py's pre-028 downgrade+seed. Runs against a
SQLite temp file (so data survives close/reopen) and, when TEST_DATABASE_URL is
set, PostgreSQL.
"""

import os
import tempfile
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.store import SqlStore
from hockey_scheduler.store.sql_store import migrate

_VERSION = "028_competition_reset"


def _sql_targets():
    """(label, url, is_shared) for each backend under test."""
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        return [("postgres", url, True)]
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return [("sqlite", path, False)]


def _exec(store, sql, params=()):
    store.conn.cursor().execute(store.dialect.sql(sql), params)


def _downgrade_028(store):
    """Reverse migration 028 to the pre-028 schema and un-record it, so a
    re-migrate re-applies it over legacy-shaped rows (as an existing install)."""
    with store.transaction():
        cur = store.conn.cursor()
        cur.execute("ALTER TABLE games DROP COLUMN league_id")
        cur.execute("ALTER TABLE season_team_registrations DROP COLUMN league_id")
        cur.execute("ALTER TABLE divisions RENAME COLUMN league_id TO level_id")
        cur.execute("DROP INDEX IF EXISTS ix_leagues_external_ref")
        cur.execute("ALTER TABLE leagues RENAME TO levels")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_levels_external_ref "
                    "ON levels(external_ref)")
        cur.execute("DROP INDEX IF EXISTS ix_teams_program")
        cur.execute("ALTER TABLE teams RENAME COLUMN program_id TO league_id")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_teams_league ON teams(league_id)")
        cur.execute("DROP INDEX IF EXISTS ix_seasons_program")
        cur.execute("ALTER TABLE seasons RENAME COLUMN program_id TO league_id")
        cur.execute("DROP INDEX IF EXISTS ix_programs_operator_organization")
        cur.execute("DROP INDEX IF EXISTS ix_programs_external_ref")
        cur.execute("ALTER TABLE programs RENAME TO leagues")
        cur.execute("ALTER TABLE leagues "
                    "RENAME COLUMN operator_organization_id TO organization_id")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_leagues_organization "
                    "ON leagues(organization_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_leagues_external_ref "
                    "ON leagues(external_ref)")
        cur.execute(store.dialect.sql(
            "DELETE FROM schema_migrations WHERE version = ?"), (_VERSION,))


def _seed_pre028(store):
    """Representative pre-028 rows covering the full competition chain."""
    with store.transaction():
        _exec(store, "INSERT INTO leagues (id, name, organization_id) "
              "VALUES (?, ?, ?)", ("prog1", "Program One", "org1"))
        _exec(store, "INSERT INTO seasons (id, league_id, name) VALUES (?, ?, ?)",
              ("s1", "prog1", "Fall 2026"))
        _exec(store, "INSERT INTO levels (id, season_id, name) VALUES (?, ?, ?)",
              ("lg1", "s1", "League One"))
        _exec(store, "INSERT INTO divisions (id, season_id, name, level_id) "
              "VALUES (?, ?, ?, ?)", ("d1", "s1", "Div A", "lg1"))
        _exec(store, "INSERT INTO teams (id, name, league_id) VALUES (?, ?, ?)",
              ("tm1", "Team One", "prog1"))
        _exec(store, "INSERT INTO season_team_registrations "
              "(id, season_id, team_id, division_id, active) VALUES (?, ?, ?, ?, 1)",
              ("r1", "s1", "tm1", "d1"))
        _exec(store, "INSERT INTO games (id, season_id, division_id, "
              "home_team_id) VALUES (?, ?, ?, ?)", ("g1", "s1", "d1", "tm1"))


class C1bUpgradedReopenTest(unittest.TestCase):
    def setUp(self):
        self._targets = _sql_targets()

    def tearDown(self):
        for label, url, is_shared in self._targets:
            if is_shared:
                SqlStore(url).reset_schema()
            elif os.path.exists(url):
                os.remove(url)

    def test_upgraded_database_reopens_with_canonical_relationships(self):
        for label, url, is_shared in self._targets:
            with self.subTest(backend=label):
                # 1. Build pre-028, seed, migrate forward, then drop the process.
                first = SqlStore(url)
                if is_shared:
                    first.reset_schema()
                _downgrade_028(first)
                _seed_pre028(first)
                migrate(first.conn, first.dialect)  # apply 028
                self.assertIn(_VERSION, first.migration_status()["applied"], label)
                first.close()

                # 2. A fresh store against the same database (a restart) reads
                #    every canonical entity + relationship back.
                store = SqlStore(url)
                try:
                    self.assertIn(_VERSION, store.migration_status()["applied"], label)

                    prog = store.get_program("prog1")
                    self.assertIsNotNone(prog, label)
                    self.assertEqual(prog.operator_organization_id, "org1", label)

                    season = store.get_season("s1")
                    self.assertEqual(season.program_id, "prog1", label)

                    league = store.get_league("lg1")
                    self.assertEqual(league.season_id, "s1", label)

                    division = store.get_division("d1")
                    self.assertEqual(division.season_id, "s1", label)
                    self.assertEqual(division.league_id, "lg1", label)

                    team = store.get_team("tm1")
                    self.assertEqual(team.program_id, "prog1", label)

                    reg = store.get_season_team_registration("r1")
                    self.assertEqual(reg.season_id, "s1", label)
                    self.assertEqual(reg.team_id, "tm1", label)
                    self.assertEqual(reg.division_id, "d1", label)
                    self.assertEqual(reg.league_id, "lg1", label)  # backfilled

                    game = store.get_game("g1")
                    self.assertEqual(game.season_id, "s1", label)
                    self.assertEqual(game.division_id, "d1", label)
                    self.assertEqual(game.league_id, "lg1", label)  # backfilled
                finally:
                    store.close()


if __name__ == "__main__":
    unittest.main()
