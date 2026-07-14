"""Competition-model reset — migration 028 (#233 Slice C1b).

Migration 028 renames the umbrella ``leagues`` table to ``programs`` (owner
column ``organization_id`` -> ``operator_organization_id``), promotes the old
``levels`` grouping to ``leagues``, reparents every division/registration/game
onto a competition League, and adds the derived ``league_id`` to registrations
and games. It is forward-only and id-stable.

Modeled on test_result_game_fk's dual-backend + downgrade + re-migrate pattern:
 - a fresh install already has the new schema;
 - a historical upgrade from the pre-028 schema renames, reparents and backfills
   correctly while preserving every id and row;
 - the C1a ambiguity gate aborts a dirty upgrade (MigrationDataError), leaving
   the pre-028 data and schema unchanged.

Runs on SQLite and (when TEST_DATABASE_URL is set) PostgreSQL.
"""

import os
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.store import SqlStore
from hockey_scheduler.store.integrity_checks import MigrationDataError
from hockey_scheduler.store.sql_store import migrate

_VERSION = "028_competition_reset"


def _sql_backends():
    backends = [("sqlite", ":memory:")]
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        backends.append(("postgres", url))
    return backends


def _fresh(url):
    store = SqlStore(url)
    if url != ":memory:":
        store.reset_schema()
    return store


def _cols(store, table):
    cur = store.conn.cursor()
    if store.backend == "sqlite":
        cur.execute(f"PRAGMA table_info({table})")
        return {r["name"] for r in cur.fetchall()}
    cur.execute("SELECT column_name FROM information_schema.columns "
                "WHERE table_name = %s", (table,))
    return {r["column_name"] for r in cur.fetchall()}


def _table_exists(store, table):
    cur = store.conn.cursor()
    if store.backend == "sqlite":
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table,))
    else:
        cur.execute("SELECT table_name FROM information_schema.tables "
                    "WHERE table_name = %s", (table,))
    return cur.fetchone() is not None


def _downgrade_028(store):
    """Reverse migration 028 to the pre-028 schema and un-record it, so a
    re-migrate re-applies it over legacy-shaped data."""
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


def _exec(store, sql, params=()):
    store.conn.cursor().execute(store.dialect.sql(sql), params)


def _one(store, sql, params=()):
    cur = store.conn.cursor()
    cur.execute(store.dialect.sql(sql), params)
    return cur.fetchone()


def _seed_pre028_clean(store):
    """A gate-clean pre-028 dataset covering every reparent path, with stable
    ids so the upgrade's id-preservation can be asserted."""
    with store.transaction():
        _exec(store, "INSERT INTO leagues (id, name, organization_id) "
              "VALUES (?, ?, ?)", ("prog1", "Program One", "org1"))
        _exec(store, "INSERT INTO seasons (id, league_id, name) VALUES (?, ?, ?)",
              ("s1", "prog1", "Season One"))
        _exec(store, "INSERT INTO levels (id, season_id, name) VALUES (?, ?, ?)",
              ("lg1", "s1", "League One"))
        _exec(store, "INSERT INTO divisions (id, season_id, name, level_id) "
              "VALUES (?, ?, ?, ?)", ("d_lv", "s1", "Div Leveled", "lg1"))
        _exec(store, "INSERT INTO divisions (id, season_id, name, level_id) "
              "VALUES (?, ?, ?, ?)", ("d_none", "s1", "Div Levelless", None))
        _exec(store, "INSERT INTO teams (id, name, league_id) VALUES (?, ?, ?)",
              ("tm1", "Team One", "prog1"))
        _exec(store, "INSERT INTO teams (id, name, league_id) VALUES (?, ?, ?)",
              ("tm2", "Team Two", "prog1"))
        _exec(store, "INSERT INTO season_team_registrations "
              "(id, season_id, team_id, division_id, active) VALUES (?, ?, ?, ?, 1)",
              ("r_div", "s1", "tm1", "d_lv"))
        _exec(store, "INSERT INTO season_team_registrations "
              "(id, season_id, team_id, division_id, active) VALUES (?, ?, ?, ?, 1)",
              ("r_nodiv", "s1", "tm2", None))
        _exec(store, "INSERT INTO games (id, division_id) VALUES (?, ?)",
              ("g_div", "d_lv"))
        _exec(store, "INSERT INTO games (id, division_id) VALUES (?, ?)",
              ("g_nodiv", None))


class FreshInstallSchemaTest(unittest.TestCase):
    def test_fresh_install_has_the_new_schema(self):
        for label, url in _sql_backends():
            with self.subTest(backend=label):
                store = _fresh(url)
                try:
                    self.assertTrue(_table_exists(store, "programs"), label)
                    self.assertFalse(_table_exists(store, "levels"), label)
                    self.assertIn("operator_organization_id",
                                  _cols(store, "programs"), label)
                    self.assertIn("program_id", _cols(store, "seasons"), label)
                    self.assertIn("league_id", _cols(store, "divisions"), label)
                    self.assertIn("program_id", _cols(store, "teams"), label)
                    self.assertIn("league_id",
                                  _cols(store, "season_team_registrations"), label)
                    self.assertIn("league_id", _cols(store, "games"), label)
                    self.assertIn(_VERSION, store.migration_status()["applied"], label)
                finally:
                    store.close()


class HistoricalUpgradeTest(unittest.TestCase):
    def test_upgrade_renames_reparents_and_preserves_rows(self):
        for label, url in _sql_backends():
            with self.subTest(backend=label):
                store = _fresh(url)
                try:
                    _downgrade_028(store)
                    _seed_pre028_clean(store)
                    migrate(store.conn, store.dialect)  # re-apply 028
                    self.assertIn(_VERSION, store.migration_status()["applied"], label)

                    # Umbrella renamed to programs; owner column renamed; id kept.
                    self.assertTrue(_table_exists(store, "programs"), label)
                    self.assertFalse(_table_exists(store, "levels"), label)
                    prog = _one(store, "SELECT id, operator_organization_id AS oo "
                                "FROM programs WHERE id = ?", ("prog1",))
                    self.assertEqual((prog["id"], prog["oo"]), ("prog1", "org1"), label)

                    # Season reparented onto program_id (id kept).
                    se = _one(store, "SELECT program_id FROM seasons WHERE id = ?",
                              ("s1",))
                    self.assertEqual(se["program_id"], "prog1", label)

                    # Grouping promoted to leagues (id kept).
                    lg = _one(store, "SELECT id FROM leagues WHERE id = ?", ("lg1",))
                    self.assertEqual(lg["id"], "lg1", label)

                    # Divisions: the leveled one keeps its league; the level-less
                    # one is backfilled from the season's sole league.
                    self.assertEqual(
                        _one(store, "SELECT league_id FROM divisions WHERE id = ?",
                             ("d_lv",))["league_id"], "lg1", label)
                    self.assertEqual(
                        _one(store, "SELECT league_id FROM divisions WHERE id = ?",
                             ("d_none",))["league_id"], "lg1", label)

                    # Team reparented onto program_id (id kept).
                    self.assertEqual(
                        _one(store, "SELECT program_id FROM teams WHERE id = ?",
                             ("tm1",))["program_id"], "prog1", label)

                    # Registration league_id: from the division's league when it
                    # has one, else the season's sole league.
                    self.assertEqual(
                        _one(store, "SELECT league_id FROM "
                             "season_team_registrations WHERE id = ?",
                             ("r_div",))["league_id"], "lg1", label)
                    self.assertEqual(
                        _one(store, "SELECT league_id FROM "
                             "season_team_registrations WHERE id = ?",
                             ("r_nodiv",))["league_id"], "lg1", label)

                    # Game league_id backfilled from its division; a division-less
                    # game stays NULL.
                    self.assertEqual(
                        _one(store, "SELECT league_id FROM games WHERE id = ?",
                             ("g_div",))["league_id"], "lg1", label)
                    self.assertIsNone(
                        _one(store, "SELECT league_id FROM games WHERE id = ?",
                             ("g_nodiv",))["league_id"], label)

                    # No rows lost.
                    for table, n in (("programs", 1), ("seasons", 1), ("leagues", 1),
                                     ("divisions", 2), ("teams", 2),
                                     ("season_team_registrations", 2), ("games", 2)):
                        self.assertEqual(
                            _one(store, f"SELECT COUNT(*) AS n FROM {table}")["n"], n,
                            f"{label}:{table}")
                finally:
                    store.close()


class AmbiguityGateTest(unittest.TestCase):
    def test_ambiguous_upgrade_aborts_and_leaves_data_unchanged(self):
        for label, url in _sql_backends():
            with self.subTest(backend=label):
                store = _fresh(url)
                try:
                    _downgrade_028(store)
                    # A level-less division in a season with TWO leagues cannot be
                    # deterministically reparented — the C1a gate must abort.
                    with store.transaction():
                        _exec(store, "INSERT INTO leagues (id, name) VALUES (?, ?)",
                              ("prog1", "Program One"))
                        _exec(store, "INSERT INTO seasons (id, league_id, name) "
                              "VALUES (?, ?, ?)", ("s1", "prog1", "S1"))
                        _exec(store, "INSERT INTO levels (id, season_id, name) "
                              "VALUES (?, ?, ?)", ("la", "s1", "La"))
                        _exec(store, "INSERT INTO levels (id, season_id, name) "
                              "VALUES (?, ?, ?)", ("lb", "s1", "Lb"))
                        _exec(store, "INSERT INTO divisions "
                              "(id, season_id, name, level_id) VALUES (?, ?, ?, ?)",
                              ("d_amb", "s1", "Ambiguous", None))

                    with self.assertRaises(MigrationDataError, msg=label) as ctx:
                        migrate(store.conn, store.dialect)
                    self.assertIn("d_amb", str(ctx.exception), label)

                    # The abort left the pre-028 schema and data untouched: no
                    # rename happened and 028 is not recorded.
                    self.assertTrue(_table_exists(store, "levels"), label)
                    self.assertFalse(_table_exists(store, "programs"), label)
                    self.assertIn("level_id", _cols(store, "divisions"), label)
                    self.assertNotIn(_VERSION, store.migration_status()["applied"],
                                     label)
                    self.assertEqual(
                        _one(store, "SELECT COUNT(*) AS n FROM divisions")["n"], 1,
                        label)
                finally:
                    store.close()


if __name__ == "__main__":
    unittest.main()
