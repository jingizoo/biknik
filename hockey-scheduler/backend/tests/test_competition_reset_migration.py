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

from helpers import BACKEND, suspend_program_org_fks  # noqa: F401

from hockey_scheduler.store import SqlStore
from hockey_scheduler.store.integrity_checks import MigrationDataError
from hockey_scheduler.store.sql_store import migrate

_VERSION = "028_competition_reset"
_V035 = "035_competition_hierarchy_reset"
_V050 = "050_schedule_scenarios"
_V052 = "052_season_roster_membership"


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
    # Migration 042's seasons.program_id → programs FK would reject the legacy
    # dangling rows these historical-upgrade / gate-abort tests plant before
    # re-running the 028/035 migrations; suspend it so the pre-042 data can be
    # modeled (#201 Slice 4). migrate() manages the SQLite pragma itself.
    suspend_program_org_fks(store)
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


def _downgrade_035(store):
    """Reverse migration 035 (#283 competition-hierarchy reset) back to the
    POST-028 schema and un-record it, so a subsequent ``_downgrade_028`` can
    finish reversing to pre-028 and a re-``migrate`` re-applies 028 AND 035 over
    legacy-shaped data. Runs on a freshly-migrated (empty) database, so only the
    SCHEMA is reversed — 035 added ``league_seasons`` + ``teams.league_id`` +
    ``leagues.program_id`` and folded ``divisions``/registration
    ``season_id``+``league_id`` into ``league_season_id``; this restores those.
    Later tables that reference the 035 hierarchy are removed and un-recorded
    too, so the forward migration replay rebuilds a complete canonical schema."""
    with store.transaction():
        cur = store.conn.cursor()
        # 050 references league_seasons (and the other permanent scope tables).
        # PostgreSQL therefore requires the dependent table to be removed before
        # this historical test can rewind 035. Replaying 050 below proves the
        # current schema still composes over the legacy upgrade path.
        cur.execute("DROP TABLE IF EXISTS schedule_scenarios")
        # 058 (#273) age_eligibility_rules ALSO references league_seasons, for
        # the SAME PostgreSQL reason -- but unlike schedule_scenarios, 058 is
        # NOT un-recorded below: it also ALTERs the (unrelated-to-this-test)
        # players table, and those columns are never reversed here, so
        # replaying 058 in full would fail on "column already exists". 058
        # stays recorded as applied (its players-side effects are genuinely
        # still there and correct); only its league_seasons-dependent TABLE
        # is dropped to unblock this historical rewind. age_eligibility_rules
        # itself does not come back after the replay below -- harmless, since
        # nothing in this migration-history suite reads or writes it.
        cur.execute("DROP TABLE IF EXISTS age_eligibility_rules")
        # 052's membership tables (#205 Slice A) are later hierarchy children
        # too; rewind them the same way, and un-record 052 below so the
        # forward replay rebuilds them (proving 052 composes over the legacy
        # upgrade path exactly as 050 does).
        cur.execute("DROP TABLE IF EXISTS season_roster_membership_events")
        cur.execute("DROP TABLE IF EXISTS season_roster_memberships")
        cur.execute("DROP INDEX IF EXISTS ix_teams_league")
        cur.execute("ALTER TABLE teams DROP COLUMN league_id")
        cur.execute("DROP INDEX IF EXISTS ux_team_league_season")
        cur.execute("DROP INDEX IF EXISTS ix_reg_league_season_division")
        cur.execute("ALTER TABLE season_team_registrations "
                    "DROP COLUMN league_season_id")
        cur.execute("ALTER TABLE season_team_registrations ADD COLUMN season_id TEXT")
        cur.execute("ALTER TABLE season_team_registrations ADD COLUMN league_id TEXT")
        cur.execute("DROP INDEX IF EXISTS ix_divisions_league_season")
        cur.execute("ALTER TABLE divisions DROP COLUMN league_season_id")
        cur.execute("ALTER TABLE divisions ADD COLUMN season_id TEXT")
        cur.execute("ALTER TABLE divisions ADD COLUMN league_id TEXT")
        cur.execute("DROP INDEX IF EXISTS ix_leagues_program")
        cur.execute("ALTER TABLE leagues DROP COLUMN program_id")
        cur.execute("ALTER TABLE leagues ADD COLUMN season_id TEXT")
        cur.execute("DROP INDEX IF EXISTS ux_league_season")
        cur.execute("DROP TABLE IF EXISTS league_seasons")
        cur.execute(store.dialect.sql(
            "DELETE FROM schema_migrations WHERE version IN (?, ?, ?)"),
            (_V035, _V050, _V052))


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
        # The umbrella references org1 as its operator; seed that organization so
        # the row is valid all the way through migration 042's
        # programs.operator_organization_id → organizations FK (#201 Slice 4) —
        # the re-migrate's foreign_key_check gate would otherwise flag it.
        _exec(store, "INSERT INTO organizations (id, name) VALUES (?, ?)",
              ("org1", "Org One"))
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
                    # #283: League is a permanent Program child with a Season
                    # overlay via league_seasons.
                    self.assertTrue(_table_exists(store, "league_seasons"), label)
                    self.assertIn("operator_organization_id",
                                  _cols(store, "programs"), label)
                    self.assertIn("program_id", _cols(store, "seasons"), label)
                    self.assertIn("program_id", _cols(store, "leagues"), label)
                    self.assertIn("league_season_id", _cols(store, "divisions"), label)
                    self.assertNotIn("league_id", _cols(store, "divisions"), label)
                    self.assertIn("program_id", _cols(store, "teams"), label)
                    self.assertIn("league_id", _cols(store, "teams"), label)
                    self.assertIn("league_season_id",
                                  _cols(store, "season_team_registrations"), label)
                    self.assertNotIn("league_id",
                                     _cols(store, "season_team_registrations"), label)
                    self.assertIn("league_id", _cols(store, "games"), label)
                    self.assertIn(_VERSION, store.migration_status()["applied"], label)
                    self.assertIn(_V035, store.migration_status()["applied"], label)
                finally:
                    _teardown(store)


class HistoricalUpgradeTest(unittest.TestCase):
    def test_upgrade_renames_reparents_and_preserves_rows(self):
        for label, url in _sql_backends():
            with self.subTest(backend=label):
                store = _fresh(url)
                try:
                    _downgrade_035(store)
                    _downgrade_028(store)
                    _seed_pre028_clean(store)
                    migrate(store.conn, store.dialect)  # re-apply 028 AND 035
                    self.assertIn(_VERSION, store.migration_status()["applied"], label)
                    self.assertIn(_V035, store.migration_status()["applied"], label)

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

                    # #283: the promoted grouping is now a PERMANENT League — a
                    # child of the Program, not the Season. The merged canonical
                    # League keeps id lg1 (lowest id wins) and is reparented onto
                    # program_id.
                    lg = _one(store, "SELECT id, program_id FROM leagues "
                              "WHERE id = ?", ("lg1",))
                    self.assertEqual((lg["id"], lg["program_id"]), ("lg1", "prog1"),
                                     label)

                    # A LeagueSeason expresses lg1's participation in s1, with the
                    # deterministic id 'ls_'<league id>.
                    ls = _one(store, "SELECT id, league_id, season_id FROM "
                              "league_seasons WHERE id = ?", ("ls_lg1",))
                    self.assertEqual(
                        (ls["id"], ls["league_id"], ls["season_id"]),
                        ("ls_lg1", "lg1", "s1"), label)

                    # Divisions now hang off the LeagueSeason: the leveled one via
                    # its own league, the level-less one via the season's sole
                    # league — both resolve to ls_lg1.
                    self.assertEqual(
                        _one(store, "SELECT league_season_id FROM divisions "
                             "WHERE id = ?", ("d_lv",))["league_season_id"],
                        "ls_lg1", label)
                    self.assertEqual(
                        _one(store, "SELECT league_season_id FROM divisions "
                             "WHERE id = ?", ("d_none",))["league_season_id"],
                        "ls_lg1", label)

                    # Team reparented onto program_id (id kept) AND assigned its
                    # permanent League automatically (its registrations resolve to
                    # the single League lg1).
                    for tm in ("tm1", "tm2"):
                        row = _one(store, "SELECT program_id, league_id FROM teams "
                                   "WHERE id = ?", (tm,))
                        self.assertEqual(
                            (row["program_id"], row["league_id"]),
                            ("prog1", "lg1"), f"{label}:{tm}")

                    # Registrations hang off the LeagueSeason: from the division's
                    # league when it has one, else the season's sole league — both
                    # ls_lg1.
                    self.assertEqual(
                        _one(store, "SELECT league_season_id FROM "
                             "season_team_registrations WHERE id = ?",
                             ("r_div",))["league_season_id"], "ls_lg1", label)
                    self.assertEqual(
                        _one(store, "SELECT league_season_id FROM "
                             "season_team_registrations WHERE id = ?",
                             ("r_nodiv",))["league_season_id"], "ls_lg1", label)

                    # Game league_id: 028 backfilled it from Division/Season, then
                    # 035 repointed it onto the merged permanent League lg1.
                    self.assertEqual(
                        _one(store, "SELECT league_id FROM games WHERE id = ?",
                             ("g_div",))["league_id"], "lg1", label)
                    self.assertEqual(
                        _one(store, "SELECT league_id FROM games WHERE id = ?",
                             ("g_nodiv",))["league_id"], "lg1", label)

                    # No rows lost; the single per-Season league became one
                    # permanent League with one LeagueSeason.
                    for table, n in (("programs", 1), ("seasons", 1), ("leagues", 1),
                                     ("league_seasons", 1), ("divisions", 2),
                                     ("teams", 2), ("season_team_registrations", 2),
                                     ("games", 2)):
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
                        _downgrade_035(store)
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
                    _downgrade_035(store)
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


class GameLeagueSeasonBackfillTest(unittest.TestCase):
    """Migration 037 backfills every REGULAR Game's league_season_id from its
    unique (league_id, season_id) LeagueSeason; EXHIBITION games stay NULL
    (#283 Slice E). Simulates a pre-037 database by dropping the additive column
    and its ledger row, then re-migrates and asserts the re-derivation — a pure
    read of existing rows that changes no history. Dual-backend."""

    def test_regular_games_scoped_exhibitions_left_null(self):
        for label, url in _sql_backends():
            with self.subTest(backend=label):
                store = _fresh(url)
                try:
                    # Canonical fixtures: a LeagueSeason with one regular game
                    # and one (mislabeled) exhibition sharing the same pair.
                    with store.transaction():
                        _exec(store, "INSERT INTO programs (id, name) VALUES (?, ?)",
                              ("p1", "P1"))
                        _exec(store, "INSERT INTO seasons (id, program_id, name) "
                              "VALUES (?, ?, ?)", ("s1", "p1", "S1"))
                        _exec(store, "INSERT INTO leagues (id, name, sort_order, "
                              "program_id) VALUES (?, ?, ?, ?)", ("lg1", "Lg1", 0, "p1"))
                        _exec(store, "INSERT INTO league_seasons (id, league_id, "
                              "season_id) VALUES (?, ?, ?)", ("ls1", "lg1", "s1"))
                        _exec(store, "INSERT INTO games (id, season_id, league_id, "
                              "game_type) VALUES (?, ?, ?, ?)",
                              ("g_reg", "s1", "lg1", "regular"))
                        _exec(store, "INSERT INTO games (id, season_id, league_id, "
                              "game_type) VALUES (?, ?, ?, ?)",
                              ("g_exh", "s1", "lg1", "exhibition"))
                    # Simulate pre-037: drop the additive column + ledger row.
                    with store.transaction():
                        _exec(store, "DROP INDEX IF EXISTS ix_games_league_season")
                        _exec(store, "ALTER TABLE games DROP COLUMN league_season_id")
                        _exec(store, "DELETE FROM schema_migrations "
                              "WHERE version = ?", ("037_game_league_season",))
                    migrate(store.conn, store.dialect)  # re-adds col + backfills
                    self.assertIn("037_game_league_season",
                                  store.migration_status()["applied"], label)
                    self.assertEqual(
                        _one(store, "SELECT league_season_id FROM games WHERE id = ?",
                             ("g_reg",))["league_season_id"], "ls1", label)
                    self.assertIsNone(
                        _one(store, "SELECT league_season_id FROM games WHERE id = ?",
                             ("g_exh",))["league_season_id"], label)
                finally:
                    _teardown(store)


class GameLeagueSeasonGateAbortTest(unittest.TestCase):
    """Migration 037's pre-migration gate (assert_regular_games_resolve_league_
    season) ABORTS the upgrade when any REGULAR game cannot be bound to a
    LeagueSeason — a null league/season, or a (league, season) pair with no
    league_seasons row (#283 Slice E, blocker 1). It names the offending game,
    leaves EXHIBITIONS (which are legitimately unscoped) alone, and mutates
    nothing: the additive column is never added and 037 is not recorded.
    Dual-backend."""

    def _pre037(self, store, seed):
        """Seed rows, then simulate a pre-037 database (drop the additive
        league_season_id column + its ledger row) so migrate() re-runs the 037
        gate over ``seed``'s rows."""
        with store.transaction():
            _exec(store, "INSERT INTO programs (id, name) VALUES (?, ?)",
                  ("p1", "P1"))
            _exec(store, "INSERT INTO seasons (id, program_id, name) "
                  "VALUES (?, ?, ?)", ("s1", "p1", "S1"))
            _exec(store, "INSERT INTO leagues (id, name, sort_order, program_id) "
                  "VALUES (?, ?, ?, ?)", ("lg1", "Lg1", 0, "p1"))
            seed(store)
        with store.transaction():
            _exec(store, "DROP INDEX IF EXISTS ix_games_league_season")
            _exec(store, "ALTER TABLE games DROP COLUMN league_season_id")
            _exec(store, "DELETE FROM schema_migrations WHERE version = ?",
                  ("037_game_league_season",))

    def test_regular_game_without_league_season_aborts_no_mutation(self):
        # (label, seed) — each seeds one regular game the gate must reject.
        def _no_ls_row(store):  # pair exists but no league_seasons row
            _exec(store, "INSERT INTO games (id, season_id, league_id, game_type) "
                  "VALUES (?, ?, ?, ?)", ("g_bad", "s1", "lg1", "regular"))

        def _null_league(store):  # regular game with no league at all
            _exec(store, "INSERT INTO league_seasons (id, league_id, season_id) "
                  "VALUES (?, ?, ?)", ("ls1", "lg1", "s1"))
            _exec(store, "INSERT INTO games (id, season_id, league_id, game_type) "
                  "VALUES (?, ?, ?, ?)", ("g_bad", "s1", None, "regular"))

        for case, seed in (("no_league_season_row", _no_ls_row),
                           ("null_league", _null_league)):
            for label, url in _sql_backends():
                with self.subTest(case=case, backend=label):
                    store = _fresh(url)
                    try:
                        self._pre037(store, seed)
                        with self.assertRaises(MigrationDataError) as ctx:
                            migrate(store.conn, store.dialect)
                        self.assertIn("g_bad", str(ctx.exception),
                                      f"{case}/{label}")
                        # Zero mutation: the column is still absent and 037 is
                        # not recorded, so the operator can repair and re-run.
                        self.assertNotIn("league_season_id",
                                         _cols(store, "games"), f"{case}/{label}")
                        self.assertNotIn(
                            "037_game_league_season",
                            store.migration_status()["applied"], f"{case}/{label}")
                    finally:
                        _teardown(store)

    def test_regular_game_with_cross_league_season_division_aborts(self):
        # A regular game whose (league_id, season_id) resolves to ls1 but whose
        # Division belongs to a DIFFERENT LeagueSeason (ls2) must abort 037 — a
        # Game bound to one LeagueSeason while its Division points at another
        # would let Division and LeagueSeason standings disagree.
        for label, url in _sql_backends():
            with self.subTest(backend=label):
                store = _fresh(url)
                try:
                    with store.transaction():
                        _exec(store, "INSERT INTO programs (id, name) VALUES (?, ?)",
                              ("p1", "P1"))
                        _exec(store, "INSERT INTO seasons (id, program_id, name) "
                              "VALUES (?, ?, ?)", ("s1", "p1", "S1"))
                        _exec(store, "INSERT INTO leagues (id, name, sort_order, "
                              "program_id) VALUES (?, ?, ?, ?)", ("lg1", "Lg1", 0, "p1"))
                        _exec(store, "INSERT INTO leagues (id, name, sort_order, "
                              "program_id) VALUES (?, ?, ?, ?)", ("lg2", "Lg2", 1, "p1"))
                        _exec(store, "INSERT INTO league_seasons (id, league_id, "
                              "season_id) VALUES (?, ?, ?)", ("ls1", "lg1", "s1"))
                        _exec(store, "INSERT INTO league_seasons (id, league_id, "
                              "season_id) VALUES (?, ?, ?)", ("ls2", "lg2", "s1"))
                        _exec(store, "INSERT INTO divisions (id, name, "
                              "league_season_id) VALUES (?, ?, ?)", ("d2", "D2", "ls2"))
                        # Resolves to ls1 via (lg1, s1), but its Division is in ls2.
                        _exec(store, "INSERT INTO games (id, season_id, league_id, "
                              "division_id, game_type) VALUES (?, ?, ?, ?, ?)",
                              ("g_x", "s1", "lg1", "d2", "regular"))
                    with store.transaction():
                        _exec(store, "DROP INDEX IF EXISTS ix_games_league_season")
                        _exec(store, "ALTER TABLE games DROP COLUMN league_season_id")
                        _exec(store, "DELETE FROM schema_migrations "
                              "WHERE version = ?", ("037_game_league_season",))
                    with self.assertRaises(MigrationDataError) as ctx:
                        migrate(store.conn, store.dialect)
                    self.assertIn("g_x", str(ctx.exception), label)
                    self.assertNotIn("league_season_id", _cols(store, "games"), label)
                    self.assertNotIn(
                        "037_game_league_season",
                        store.migration_status()["applied"], label)
                finally:
                    _teardown(store)

    def test_unscoped_exhibition_does_not_abort(self):
        # An EXHIBITION with no LeagueSeason is legitimate — the gate ignores it
        # and 037 applies, leaving its league_season_id NULL.
        for label, url in _sql_backends():
            with self.subTest(backend=label):
                store = _fresh(url)
                try:
                    self._pre037(store, lambda s: _exec(
                        s, "INSERT INTO games (id, season_id, league_id, "
                        "game_type) VALUES (?, ?, ?, ?)",
                        ("g_exh", "s1", None, "exhibition")))
                    migrate(store.conn, store.dialect)  # gate passes
                    self.assertIn("037_game_league_season",
                                  store.migration_status()["applied"], label)
                    self.assertIsNone(
                        _one(store, "SELECT league_season_id FROM games "
                             "WHERE id = ?", ("g_exh",))["league_season_id"], label)
                finally:
                    _teardown(store)


class AmbiguityGateTest(unittest.TestCase):
    def test_ambiguous_upgrade_aborts_and_leaves_data_unchanged(self):
        for label, url in _sql_backends():
            with self.subTest(backend=label):
                store = _fresh(url)
                try:
                    _downgrade_035(store)
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
