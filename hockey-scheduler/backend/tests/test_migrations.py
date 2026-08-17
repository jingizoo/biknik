"""Numbered, forward-only migrations (#75).

``schema_migrations`` is authoritative: each ``NNN_name.sql`` file under
store/migrations/ runs at most once, in order, and is never re-applied once
recorded. These tests cover discovery/order, first-boot application,
idempotent reopen, adoption over a pre-#75 database, the authority of the
ledger (a recorded version is skipped even if its objects are missing), and a
guard that the hand-written SQL schema matches the SPECS the row-mapper uses.
"""

import os
import tempfile
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.store import SqlStore
from hockey_scheduler.store.sql_store import SPECS, _load_migrations


def _applied(store):
    cur = store.conn.cursor()
    cur.execute("SELECT version FROM schema_migrations ORDER BY version")
    return [r["version"] for r in cur.fetchall()]


# The full set of shipped migration versions, in order — derived from the
# files so adding a migration doesn't require editing every assertion here.
ALL_VERSIONS = [v for v, _ in _load_migrations()]


def _table_columns(store, table):
    cur = store.conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    return {r["name"] for r in cur.fetchall()}


class MigrationDiscoveryTest(unittest.TestCase):
    def test_migrations_are_discovered_in_numeric_order(self):
        versions = [v for v, _ in _load_migrations()]
        self.assertEqual(versions[:2], ["001_initial", "002_sessions"])
        self.assertEqual(versions, sorted(versions))

    def test_every_migration_has_statements(self):
        for version, statements in _load_migrations():
            self.assertTrue(statements, f"{version} parsed to zero statements")


class MigrationApplyTest(unittest.TestCase):
    def test_fresh_database_records_all_versions(self):
        store = SqlStore(":memory:")
        self.assertEqual(_applied(store), ALL_VERSIONS)

    def test_reopen_applies_nothing_new(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            SqlStore(path)
            reopened = SqlStore(path)
            # Exactly the two versions — no duplicates, nothing re-run.
            self.assertEqual(_applied(reopened), ALL_VERSIONS)
        finally:
            os.remove(path)

    def test_adoption_over_legacy_marker_is_safe(self):
        # A pre-#75 database recorded a single '0001_initial' marker and already
        # has the core tables — but not schema added by later numbered
        # migrations. Booting the numbered runner must backfill every version
        # without error: CREATE ... IF NOT EXISTS is safe over existing tables,
        # and additive ALTERs land because a legacy DB genuinely lacks those
        # columns. We simulate that legacy shape by dropping the #80 columns.
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            store = SqlStore(path)
            cur = store.conn.cursor()
            # Reverse migrations 035 (#283) then 028 (#233 C1b) first so the DB is
            # back at the pre-028 competition-model names a legacy adopter would
            # carry. The legacy-strip below then operates on those original names,
            # and the re-boot re-applies 001-035 (028 renaming, 035 resetting the
            # hierarchy again).
            #
            # 035 folded divisions/registration season_id+league_id into
            # league_season_id, made leagues a permanent Program child
            # (program_id, no season_id), and added teams.league_id +
            # league_seasons. Reverse those to the post-028 shape the 028-reversal
            # below expects (drop teams.league_id so program_id can be renamed
            # back onto it without collision).
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
            # 050 is a later dependent of the 035 hierarchy being reversed.
            # Remove it before league_seasons; deleting the migration ledger
            # below makes the adoption replay rebuild it in canonical order.
            cur.execute("DROP TABLE IF EXISTS schedule_scenarios")
            cur.execute("DROP TABLE IF EXISTS league_seasons")
            cur.execute("ALTER TABLE games DROP COLUMN league_id")
            cur.execute("ALTER TABLE season_team_registrations DROP COLUMN league_id")
            cur.execute("ALTER TABLE divisions RENAME COLUMN league_id TO level_id")
            cur.execute("DROP INDEX IF EXISTS ix_leagues_external_ref")
            cur.execute("ALTER TABLE leagues RENAME TO levels")
            cur.execute("CREATE INDEX ix_levels_external_ref ON levels(external_ref)")
            cur.execute("DROP INDEX IF EXISTS ix_teams_program")
            cur.execute("ALTER TABLE teams RENAME COLUMN program_id TO league_id")
            cur.execute("CREATE INDEX ix_teams_league ON teams(league_id)")
            cur.execute("DROP INDEX IF EXISTS ix_seasons_program")
            cur.execute("ALTER TABLE seasons RENAME COLUMN program_id TO league_id")
            cur.execute("DROP INDEX IF EXISTS ix_programs_operator_organization")
            cur.execute("DROP INDEX IF EXISTS ix_programs_external_ref")
            cur.execute("ALTER TABLE programs RENAME TO leagues")
            cur.execute("ALTER TABLE leagues "
                        "RENAME COLUMN operator_organization_id TO organization_id")
            cur.execute("CREATE INDEX ix_leagues_organization ON leagues(organization_id)")
            cur.execute("CREATE INDEX ix_leagues_external_ref ON leagues(external_ref)")
            for col in ("last_attempt_at", "next_attempt_at", "dead_lettered_at"):
                cur.execute(f"ALTER TABLE notification_deliveries DROP COLUMN {col}")
            cur.execute("ALTER TABLE games DROP COLUMN is_draft")  # #86 additive col
            cur.execute("ALTER TABLE teams DROP COLUMN external_ref")  # #93 additive col
            cur.execute("ALTER TABLE players DROP COLUMN external_ref")  # #93 additive col
            # #331 review round 11: officials.external_ref and rinks.external_ref
            # each gained a unique index (migrations 047/048) after #94/#95
            # first added the columns, so a legacy DB has neither the index
            # nor the column -- same as the #173/#174 columns below, drop the
            # index before the indexed column (SQLite rejects dropping one).
            cur.execute("DROP INDEX IF EXISTS ux_officials_external_ref")
            cur.execute("ALTER TABLE officials DROP COLUMN external_ref")  # #94 additive col
            cur.execute("DROP INDEX IF EXISTS ux_rinks_external_ref")
            cur.execute("ALTER TABLE rinks DROP COLUMN external_ref")  # #95 additive col
            cur.execute("ALTER TABLE venues DROP COLUMN organization_id")  # #166 additive col
            cur.execute("ALTER TABLE divisions DROP COLUMN level_id")  # #166 additive col
            # #173 columns carry indexes; a pre-#173 DB has neither, so drop the
            # indexes before the columns (SQLite rejects dropping an indexed col).
            cur.execute("DROP INDEX IF EXISTS ix_leagues_organization")  # #173
            cur.execute("DROP INDEX IF EXISTS ix_venues_league")  # #173
            cur.execute("ALTER TABLE leagues DROP COLUMN organization_id")  # #173 additive col
            cur.execute("ALTER TABLE venues DROP COLUMN league_id")  # #173 additive col
            # #174 PR E hierarchy external_ref columns each carry an index, so
            # drop the index before the indexed column (SQLite rejects
            # dropping an indexed col), same as the #173 columns above.
            for tbl in ("organizations", "leagues", "venues", "seasons",
                        "levels", "divisions"):
                cur.execute(f"DROP INDEX IF EXISTS ix_{tbl}_external_ref")
                cur.execute(f"ALTER TABLE {tbl} DROP COLUMN external_ref")
            # #180 permanent teams: teams.league_id is indexed, and
            # season_team_registrations is a brand-new table a pre-#180 DB lacks.
            cur.execute("DROP INDEX IF EXISTS ix_teams_league")
            cur.execute("ALTER TABLE teams DROP COLUMN league_id")
            cur.execute("DROP TABLE IF EXISTS season_team_registrations")
            cur.execute("ALTER TABLE guardian_links DROP COLUMN consent_method")  # #35
            cur.execute("ALTER TABLE guardian_links DROP COLUMN consented_at")  # #35
            cur.execute("ALTER TABLE calendar_feed_tokens DROP COLUMN created_by")  # #131
            cur.execute("ALTER TABLE calendar_feed_tokens DROP COLUMN last_used_at")  # #131
            cur.execute("ALTER TABLE calendar_feed_tokens DROP COLUMN revoked_by")  # #131
            cur.execute("ALTER TABLE contact_destinations DROP COLUMN active")  # #232 review 4
            cur.execute("ALTER TABLE notification_preferences DROP COLUMN active")  # #232 review 4
            cur.execute("DROP INDEX IF EXISTS ix_clubs_external_ref")  # #260 Slice F
            cur.execute("ALTER TABLE clubs DROP COLUMN external_ref")  # #260 Slice F additive col
            cur.execute("DROP INDEX IF EXISTS ix_games_game_type")  # #283 Slice D
            cur.execute("ALTER TABLE games DROP COLUMN game_type")  # #283 Slice D additive col
            cur.execute("DROP INDEX IF EXISTS ix_games_league_season")  # #283 Slice E
            cur.execute("ALTER TABLE games DROP COLUMN league_season_id")  # #283 Slice E additive col
            cur.execute("ALTER TABLE seasons DROP COLUMN status")  # #159 additive col
            cur.execute("ALTER TABLE seasons DROP COLUMN archived_at")  # #159 additive col
            # #345 migration 049: the persistent League axis on the per-user
            # context row. A legacy adopter has 044's table without this column,
            # so drop it here — otherwise adoption re-runs 049's ALTER against a
            # column that already exists and fails with "duplicate column name".
            cur.execute(
                "ALTER TABLE user_active_context DROP COLUMN league_id")
            # #159 review findings 2+5, migration 051: the persisted switch
            # generation on the same row. Same reasoning as league_id above —
            # drop it so adoption's re-run of 051's ALTER lands on a column
            # that genuinely does not exist yet.
            cur.execute(
                "ALTER TABLE user_active_context DROP COLUMN generation")
            # #423 round-N review finding 1, migration 052: the epoch fence's
            # persisted version-counter table is a brand-new CREATE TABLE, the
            # same shape as schedule_scenarios (050) above — drop it so
            # adoption's re-run of 052 lands on a table that genuinely does
            # not exist yet, rather than "table already exists".
            cur.execute("DROP TABLE IF EXISTS epoch_fence_version")
            # #159 review round 2, migration 053: the copy-forward commit
            # idempotency ledger is another brand-new CREATE TABLE, same
            # shape as epoch_fence_version (052) immediately above — drop it
            # (its UNIQUE index goes with it) so adoption's re-run of 053
            # lands on a table that genuinely does not exist yet.
            cur.execute("DROP TABLE IF EXISTS season_copy_forward_commits")
            cur.execute("DELETE FROM schema_migrations")
            cur.execute("INSERT INTO schema_migrations(version, applied_at) "
                        "VALUES ('0001_initial', '2026-01-01')")
            adopted = SqlStore(path)
            self.assertEqual(_applied(adopted), ["0001_initial"] + ALL_VERSIONS)
            # The ALTER migration re-added its columns during adoption.
            self.assertTrue(
                {"last_attempt_at", "next_attempt_at", "dead_lettered_at"}
                <= _table_columns(adopted, "notification_deliveries"))
            self.assertIn("external_ref", _table_columns(adopted, "teams"))
            self.assertTrue(  # #159 season lifecycle re-added on adoption
                {"status", "archived_at"} <= _table_columns(adopted, "seasons"))
            self.assertIn("external_ref", _table_columns(adopted, "players"))
            self.assertIn("external_ref", _table_columns(adopted, "officials"))
            self.assertIn("external_ref", _table_columns(adopted, "rinks"))
            self.assertTrue(
                {"consent_method", "consented_at"}
                <= _table_columns(adopted, "guardian_links"))
            self.assertTrue(
                {"created_by", "last_used_at", "revoked_by"}
                <= _table_columns(adopted, "calendar_feed_tokens"))
            self.assertIn("active", _table_columns(adopted, "contact_destinations"))
            self.assertIn("active", _table_columns(adopted, "notification_preferences"))
            self.assertIn("external_ref", _table_columns(adopted, "clubs"))  # #260
            self.assertIn("organization_id", _table_columns(adopted, "venues"))
            # #233 C1b: the umbrella is now `programs` with operator_organization_id.
            self.assertIn("operator_organization_id",
                          _table_columns(adopted, "programs"))
            self.assertIn("league_id", _table_columns(adopted, "venues"))
            # #159 review findings 2+5, migration 051: the persisted switch
            # generation re-added on the per-user context row.
            self.assertIn("generation",
                          _table_columns(adopted, "user_active_context"))
            # #174 PR E hierarchy external_ref columns re-landed on every table
            # (post-028 names: `programs` umbrella, `leagues` grouping).
            for tbl in ("organizations", "programs", "venues", "seasons",
                        "leagues", "divisions"):
                self.assertIn("external_ref", _table_columns(adopted, tbl))
            # #180 permanent-teams column (now program_id) + registrations table.
            self.assertIn("program_id", _table_columns(adopted, "teams"))
            # #283 migration 035: registrations hang off a LeagueSeason
            # (league_season_id), season_id + league_id retired.
            self.assertIn("league_season_id",
                          _table_columns(adopted, "season_team_registrations"))
            self.assertNotIn("season_id",
                             _table_columns(adopted, "season_team_registrations"))
            self.assertNotIn("league_id",
                             _table_columns(adopted, "season_team_registrations"))
            # #283 migration 035: League is a permanent Program child with a
            # Season overlay via league_seasons, and Teams gain a permanent
            # League. Games keep their (repointed) league_id.
            adopted_tables = {r["name"] for r in adopted.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            self.assertIn("league_seasons", adopted_tables)
            self.assertIn("schedule_scenarios", adopted_tables)
            # #423 round-N review finding 1, migration 052: the epoch fence's
            # version-counter table re-created on adoption. No rows are
            # pre-seeded (each fence key gets a row on its first bump — see
            # the migration's own docstring), so an unbumped key correctly
            # reads back 0 on a freshly-adopted database.
            self.assertIn("epoch_fence_version", adopted_tables)
            self.assertEqual(
                adopted.current_epoch_fence_version("some-key-nobody-bumped"),
                0,
                "an unbumped key on a freshly-adopted database must read 0, "
                "not raise or read something stale")
            # #159 review round 2, migration 053: the copy-forward commit
            # idempotency ledger re-created on adoption, same shape as the
            # epoch fence check immediately above.
            self.assertIn("season_copy_forward_commits", adopted_tables)
            self.assertIsNone(
                adopted.get_season_copy_forward_commit_by_fingerprint(
                    "some-fingerprint-nobody-committed"),
                "an unknown fingerprint on a freshly-adopted database must "
                "read None, not raise or read something stale")
            # #159 review round 3, migration 054: the immutable response
            # snapshot column re-added on adoption too -- the whole table was
            # dropped above (pre-053 shape), so 054's ADD COLUMN lands on a
            # freshly re-created table exactly as it would on a genuinely
            # legacy database that stopped at 053.
            self.assertIn("response_snapshot",
                          _table_columns(adopted, "season_copy_forward_commits"))
            self.assertIn("league_season_id", _table_columns(adopted, "divisions"))
            self.assertIn("program_id", _table_columns(adopted, "leagues"))
            self.assertIn("league_id", _table_columns(adopted, "teams"))
            self.assertIn("league_id", _table_columns(adopted, "games"))
        finally:
            os.remove(path)

    def test_ledger_is_authoritative_not_if_not_exists(self):
        # Prove schema_migrations drives application: mark 002 as applied but
        # drop its table. A re-migrate must SKIP it (ledger wins), leaving the
        # table absent — i.e. we do not blindly re-run every file.
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            store = SqlStore(path)
            cur = store.conn.cursor()
            cur.execute("DROP TABLE sessions")
            # 002 is still recorded as applied from the first boot.
            reopened = SqlStore(path)
            cols = _table_columns(reopened, "sessions")
            self.assertEqual(cols, set(),
                             "recorded migration must not be re-applied")
        finally:
            os.remove(path)


class MigrationSchemaParityTest(unittest.TestCase):
    """The hand-written migration SQL must match the SPECS the mapper uses."""

    def test_every_spec_table_matches_migrated_columns(self):
        store = SqlStore(":memory:")
        for spec in SPECS.values():
            with self.subTest(table=spec.table):
                self.assertEqual(
                    _table_columns(store, spec.table), set(spec.names),
                    f"{spec.table} columns drifted from the SPEC")


class ResetSchemaTest(unittest.TestCase):
    def test_reset_schema_reapplies_from_scratch(self):
        store = SqlStore(":memory:")
        store.reset_schema()
        self.assertEqual(_applied(store), ALL_VERSIONS)

    def test_reset_schema_recreates_tables(self):
        store = SqlStore(":memory:")
        store.reset_schema()
        cur = store.conn.cursor()
        cur.execute("SELECT count(*) AS n FROM sessions")
        self.assertEqual(cur.fetchone()["n"], 0)


if __name__ == "__main__":
    unittest.main()
