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

from helpers import BACKEND, suspend_program_org_fks  # noqa: F401

from hockey_scheduler.store import SqlStore
from hockey_scheduler.store.sql_store import migrate

_VERSION = "028_competition_reset"
_V035 = "035_competition_hierarchy_reset"
_V050 = "050_schedule_scenarios"
_V052 = "052_season_roster_membership"


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


def _downgrade_035(store):
    """Reverse migration 035 (#283 competition-hierarchy reset) back to the
    POST-028 schema and un-record it, so ``_downgrade_028`` can finish reversing
    to pre-028 and a re-``migrate`` re-applies 028 AND 035 over legacy-shaped
    rows. Runs on a freshly-migrated (empty) database — only the SCHEMA is
    reversed. Later hierarchy dependents are rewound too so the forward replay
    restores the complete current schema."""
    with store.transaction():
        cur = store.conn.cursor()
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
        # reopen replay rebuilds them alongside 035 and 050.
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
        # Seed the operator organization so the program's operator ref stays
        # valid through migration 042's FK (#201 Slice 4); the re-migrate's
        # foreign_key_check gate would otherwise flag it.
        _exec(store, "INSERT INTO organizations (id, name) VALUES (?, ?)",
              ("org1", "Org One"))
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
                # The pre-028 seed models legacy data migration 042's FKs would
                # reject; suspend them BEFORE the downgrade renames programs/
                # leagues away, so PostgreSQL can drop the named constraints while
                # the tables still carry their canonical names (#201 Slice 4).
                suspend_program_org_fks(first)
                _downgrade_035(first)
                _downgrade_028(first)
                _seed_pre028(first)
                migrate(first.conn, first.dialect)  # apply 028, 035, and 050
                self.assertIn(_VERSION, first.migration_status()["applied"], label)
                self.assertIn(_V035, first.migration_status()["applied"], label)
                self.assertIn(_V050, first.migration_status()["applied"], label)
                first.close()

                # 2. A fresh store against the same database (a restart) reads
                #    every canonical entity + relationship back.
                store = SqlStore(url)
                try:
                    self.assertIn(_VERSION, store.migration_status()["applied"], label)
                    self.assertIn(_V035, store.migration_status()["applied"], label)
                    self.assertIn(_V050, store.migration_status()["applied"], label)

                    prog = store.get_program("prog1")
                    self.assertIsNotNone(prog, label)
                    self.assertEqual(prog.operator_organization_id, "org1", label)

                    season = store.get_season("s1")
                    self.assertEqual(season.program_id, "prog1", label)

                    # #283: League is now a permanent child of the Program; its
                    # participation in s1 is a LeagueSeason (id 'ls_'<league id>).
                    league = store.get_league("lg1")
                    self.assertEqual(league.program_id, "prog1", label)

                    league_season = store.get_league_season("ls_lg1")
                    self.assertEqual(league_season.league_id, "lg1", label)
                    self.assertEqual(league_season.season_id, "s1", label)

                    # Division hangs off the LeagueSeason, not the Season/League.
                    division = store.get_division("d1")
                    self.assertEqual(division.league_season_id, "ls_lg1", label)

                    # Team reparented onto its Program AND assigned its permanent
                    # League (its sole registration resolves to lg1).
                    team = store.get_team("tm1")
                    self.assertEqual(team.program_id, "prog1", label)
                    self.assertEqual(team.league_id, "lg1", label)

                    reg = store.get_season_team_registration("r1")
                    self.assertEqual(reg.league_season_id, "ls_lg1", label)
                    self.assertEqual(reg.team_id, "tm1", label)
                    self.assertEqual(reg.division_id, "d1", label)

                    game = store.get_game("g1")
                    self.assertEqual(game.season_id, "s1", label)
                    self.assertEqual(game.division_id, "d1", label)
                    self.assertEqual(game.league_id, "lg1", label)  # repointed
                finally:
                    store.close()


if __name__ == "__main__":
    unittest.main()
