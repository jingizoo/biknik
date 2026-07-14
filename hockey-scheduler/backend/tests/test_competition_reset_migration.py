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


def _teardown(store):
    """Unconditionally return the (possibly SHARED, e.g. Postgres) database to a
    clean canonical baseline before closing (#233 C1b item 8).

    A downgrade/abort test leaves the pre-028 shape behind — ``levels`` present,
    no ``programs`` — and on a shared database that legacy collision would persist
    into the next test. ``reset_schema()`` drops every canonical table AND the
    legacy ``levels`` table, then re-migrates to canonical, so file/CI ordering
    can never leave the database dirty. Runs in a ``finally``, so it happens even
    when the test body raised."""
    try:
        store.reset_schema()
    finally:
        store.close()


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
        # A division-less game derives its league from its Season's sole league
        # (#233 C1b item 6): give it a season so the backfill is deterministic.
        _exec(store, "INSERT INTO games (id, season_id, division_id) "
              "VALUES (?, ?, ?)", ("g_nodiv", "s1", None))


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
                    _teardown(store)


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
                    # game is backfilled from its Season's sole league (#233 C1b
                    # item 6), never left unscoped.
                    self.assertEqual(
                        _one(store, "SELECT league_id FROM games WHERE id = ?",
                             ("g_div",))["league_id"], "lg1", label)
                    self.assertEqual(
                        _one(store, "SELECT league_id FROM games WHERE id = ?",
                             ("g_nodiv",))["league_id"], "lg1", label)

                    # No rows lost.
                    for table, n in (("programs", 1), ("seasons", 1), ("leagues", 1),
                                     ("divisions", 2), ("teams", 2),
                                     ("season_team_registrations", 2), ("games", 2)):
                        self.assertEqual(
                            _one(store, f"SELECT COUNT(*) AS n FROM {table}")["n"], n,
                            f"{label}:{table}")
                finally:
                    _teardown(store)


_GATE_TABLES = ("seasons", "teams", "levels", "divisions",
                "season_team_registrations", "games")


def _count(store, table):
    return _one(store, f"SELECT COUNT(*) AS n FROM {table}")["n"]


# Each case seeds ONE offending pre-028 row (on an otherwise C1a-clean dataset)
# that the C1b gate — the rest of migration 028's rename/reparent/backfill —
# must catch. (label, seed(store), [expected substrings in the abort message]).
def _seed_season_missing_program(store):
    _exec(store, "INSERT INTO seasons (id, league_id, name) VALUES (?, ?, ?)",
          ("s_bad", "ghost_umbrella", "Orphan Season"))


def _seed_team_missing_program(store):
    _exec(store, "INSERT INTO teams (id, name, league_id) VALUES (?, ?, ?)",
          ("t_bad", "Orphan Team", "ghost_umbrella"))


def _seed_promoted_league_missing_season(store):
    _exec(store, "INSERT INTO levels (id, season_id, name) VALUES (?, ?, ?)",
          ("lv_bad", "ghost_season", "Orphan League"))


def _seed_game_dangling_division(store):
    _exec(store, "INSERT INTO games (id, division_id) VALUES (?, ?)",
          ("g_bad", "ghost_division"))


def _seed_game_cross_season_division(store):
    _exec(store, "INSERT INTO leagues (id, name) VALUES (?, ?)", ("prog1", "P1"))
    _exec(store, "INSERT INTO seasons (id, league_id, name) VALUES (?, ?, ?)",
          ("s1", "prog1", "S1"))
    _exec(store, "INSERT INTO seasons (id, league_id, name) VALUES (?, ?, ?)",
          ("s2", "prog1", "S2"))
    _exec(store, "INSERT INTO levels (id, season_id, name) VALUES (?, ?, ?)",
          ("l1", "s1", "L1"))
    _exec(store, "INSERT INTO divisions (id, season_id, name, level_id) "
          "VALUES (?, ?, ?, ?)", ("d1", "s1", "D1", "l1"))
    _exec(store, "INSERT INTO games (id, season_id, division_id) "
          "VALUES (?, ?, ?)", ("g_x", "s2", "d1"))  # division from another season


def _seed_game_missing_season(store):
    _exec(store, "INSERT INTO games (id, season_id, division_id) "
          "VALUES (?, ?, ?)", ("g_ns", "ghost_season", None))


def _seed_game_ambiguous_season(store):
    _exec(store, "INSERT INTO leagues (id, name) VALUES (?, ?)", ("prog1", "P1"))
    _exec(store, "INSERT INTO seasons (id, league_id, name) VALUES (?, ?, ?)",
          ("s1", "prog1", "S1"))
    _exec(store, "INSERT INTO levels (id, season_id, name) VALUES (?, ?, ?)",
          ("la", "s1", "La"))
    _exec(store, "INSERT INTO levels (id, season_id, name) VALUES (?, ?, ?)",
          ("lb", "s1", "Lb"))
    _exec(store, "INSERT INTO games (id, season_id, division_id) "
          "VALUES (?, ?, ?)", ("g_amb", "s1", None))  # season has 2 leagues


_C1B_GATE_CASES = [
    ("season_missing_program", _seed_season_missing_program,
     ["season s_bad", "missing_program"]),
    ("team_missing_program", _seed_team_missing_program,
     ["team t_bad", "missing_program"]),
    ("promoted_league_missing_season", _seed_promoted_league_missing_season,
     ["league lv_bad", "missing_season"]),
    ("game_dangling_division", _seed_game_dangling_division,
     ["game g_bad", "dangling_division"]),
    ("game_cross_season_division", _seed_game_cross_season_division,
     ["game g_x", "cross_season_division"]),
    ("game_missing_season", _seed_game_missing_season,
     ["game g_ns", "missing_season"]),
    ("game_ambiguous_season", _seed_game_ambiguous_season,
     ["game g_amb", "no_single_league"]),
]


class C1bGateAbortTest(unittest.TestCase):
    """The combined C1b gate (assert_competition_reset_ready_c1b) — registered as
    028's pre-migration check — aborts every rename/reparent/backfill case C1a
    doesn't cover, naming the offending row, and leaves the pre-028 data and
    schema byte-for-byte unchanged (dual-backend, no mutation)."""

    def test_each_c1b_case_aborts_without_mutation(self):
        for case, seed, expected in _C1B_GATE_CASES:
            for label, url in _sql_backends():
                with self.subTest(case=case, backend=label):
                    store = _fresh(url)
                    try:
                        _downgrade_028(store)
                        with store.transaction():
                            seed(store)
                        counts = {t: _count(store, t) for t in _GATE_TABLES}

                        with self.assertRaises(MigrationDataError) as ctx:
                            migrate(store.conn, store.dialect)  # runs the gate
                        msg = str(ctx.exception)
                        for token in expected:
                            self.assertIn(token, msg, f"{case}/{label}: {msg}")

                        # No mutation: schema still pre-028, 028 not recorded,
                        # every row count unchanged.
                        self.assertTrue(_table_exists(store, "levels"), case)
                        self.assertFalse(_table_exists(store, "programs"), case)
                        self.assertNotIn(
                            _VERSION, store.migration_status()["applied"], case)
                        self.assertEqual(
                            {t: _count(store, t) for t in _GATE_TABLES}, counts,
                            case)
                    finally:
                        _teardown(store)


class GameLeagueBackfillTest(unittest.TestCase):
    """Migration 028 backfills every Game's competition league_id: from its
    Division→League when it has a division, else from its Season's sole League
    (#233 C1b item 6). Dual-backend."""

    def test_division_backed_and_division_less_games_are_scoped(self):
        for label, url in _sql_backends():
            with self.subTest(backend=label):
                store = _fresh(url)
                try:
                    _downgrade_028(store)
                    with store.transaction():
                        _exec(store, "INSERT INTO leagues (id, name) VALUES (?, ?)",
                              ("prog1", "P1"))
                        _exec(store, "INSERT INTO seasons (id, league_id, name) "
                              "VALUES (?, ?, ?)", ("s1", "prog1", "S1"))
                        _exec(store, "INSERT INTO levels (id, season_id, name) "
                              "VALUES (?, ?, ?)", ("lg1", "s1", "Lg1"))
                        _exec(store, "INSERT INTO divisions "
                              "(id, season_id, name, level_id) VALUES (?, ?, ?, ?)",
                              ("d1", "s1", "D1", "lg1"))
                        # division-backed: league via Division→League.
                        _exec(store, "INSERT INTO games (id, season_id, division_id) "
                              "VALUES (?, ?, ?)", ("g_div", "s1", "d1"))
                        # division-less, single-league season: league via Season.
                        _exec(store, "INSERT INTO games (id, season_id, division_id) "
                              "VALUES (?, ?, ?)", ("g_nodiv", "s1", None))
                    migrate(store.conn, store.dialect)  # gate passes; 028 applies
                    self.assertIn(_VERSION, store.migration_status()["applied"], label)
                    self.assertEqual(
                        _one(store, "SELECT league_id FROM games WHERE id = ?",
                             ("g_div",))["league_id"], "lg1", label)
                    self.assertEqual(
                        _one(store, "SELECT league_id FROM games WHERE id = ?",
                             ("g_nodiv",))["league_id"], "lg1", label)
                finally:
                    _teardown(store)


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
                    _teardown(store)


if __name__ == "__main__":
    unittest.main()
