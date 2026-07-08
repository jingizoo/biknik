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
            for col in ("last_attempt_at", "next_attempt_at", "dead_lettered_at"):
                cur.execute(f"ALTER TABLE notification_deliveries DROP COLUMN {col}")
            cur.execute("ALTER TABLE games DROP COLUMN is_draft")  # #86 additive col
            cur.execute("ALTER TABLE teams DROP COLUMN external_ref")  # #93 additive col
            cur.execute("ALTER TABLE players DROP COLUMN external_ref")  # #93 additive col
            cur.execute("ALTER TABLE officials DROP COLUMN external_ref")  # #94 additive col
            cur.execute("ALTER TABLE rinks DROP COLUMN external_ref")  # #95 additive col
            cur.execute("ALTER TABLE guardian_links DROP COLUMN consent_method")  # #35
            cur.execute("ALTER TABLE guardian_links DROP COLUMN consented_at")  # #35
            cur.execute("ALTER TABLE calendar_feed_tokens DROP COLUMN created_by")  # #131
            cur.execute("ALTER TABLE calendar_feed_tokens DROP COLUMN last_used_at")  # #131
            cur.execute("ALTER TABLE calendar_feed_tokens DROP COLUMN revoked_by")  # #131
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
            self.assertIn("external_ref", _table_columns(adopted, "players"))
            self.assertIn("external_ref", _table_columns(adopted, "officials"))
            self.assertIn("external_ref", _table_columns(adopted, "rinks"))
            self.assertTrue(
                {"consent_method", "consented_at"}
                <= _table_columns(adopted, "guardian_links"))
            self.assertTrue(
                {"created_by", "last_used_at", "revoked_by"}
                <= _table_columns(adopted, "calendar_feed_tokens"))
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
