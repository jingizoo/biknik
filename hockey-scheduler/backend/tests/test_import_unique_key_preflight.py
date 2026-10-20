"""Contaminated-database preflight for migrations 047 and 048 (#336).

Migration 047 adds partial unique indexes on ``officials.external_ref`` and
``official_availability(official_id, start_time, end_time)``; 048 adds one on
``rinks.external_ref``. Each is forward-only and registers a read-only
pre-migration check in ``_PRE_MIGRATION_CHECKS``, so existing duplicates halt
the upgrade with an actionable ``MigrationDataError`` instead of an opaque
index failure.

Existing coverage proves clean upgrades only. Here each test rebuilds the
pre-migration shape on a real database (it drops that migration's indexes and
un-records its ledger row), plants the duplicate class directly, and runs the
real ``migrate()``. It asserts:
  * the upgrade halts with ``MigrationDataError`` naming the conflicting key
    (and, for availability windows, the tuple and row ids), and nothing
    unrelated;
  * neither the ledger row nor the index was written;
  * NULL-bearing rows stay allowed, exactly as the partial predicates say;
  * once the conflict is cleaned, the same ``migrate()`` applies the version
    and records it.

SQLite always runs; PostgreSQL runs when ``TEST_DATABASE_URL`` is set.
"""

import os
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.store import SqlStore
from hockey_scheduler.store.integrity_checks import MigrationDataError
from hockey_scheduler.store.sql_store import migrate

V047 = "047_official_import_unique_keys"
V048 = "048_rink_external_ref_unique"
INDEXES = {
    V047: ("ux_officials_external_ref", "ux_official_availability_official_time"),
    V048: ("ux_rinks_external_ref",),
}


def _backends():
    backends = [("sqlite", ":memory:")]
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        backends.append(("postgres", url))
    return backends


class ImportUniqueKeyPreflightTest(unittest.TestCase):

    def _each_backend(self, version, body):
        for label, url in _backends():
            with self.subTest(backend=label):
                store = SqlStore(url)
                try:
                    if url != ":memory:":
                        store.reset_schema()  # isolate on a shared database
                    self._downgrade(store, version)
                    body(store, label)
                finally:
                    # A halted migration leaves its version un-recorded over
                    # dirty rows, and the next SqlStore() on a shared
                    # database would re-run it while opening. Leave a fully
                    # migrated, empty schema behind whatever the outcome.
                    try:
                        if url != ":memory:":
                            store.reset_schema()
                    finally:
                        store.close()

    # -- database shape helpers ---------------------------------------------
    def _downgrade(self, store, version):
        """Return the database to just before ``version``: its indexes gone
        and its ledger row un-recorded, so ``migrate()`` will apply it again."""
        with store.transaction():
            cur = store.conn.cursor()
            for index in INDEXES[version]:
                cur.execute(f"DROP INDEX IF EXISTS {index}")
            cur.execute(store.dialect.sql(
                "DELETE FROM schema_migrations WHERE version = ?"), (version,))
        self.assertFalse(self._recorded(store, version))
        for index in INDEXES[version]:
            self.assertFalse(self._index_exists(store, index), index)

    def _insert(self, store, table, rows):
        with store.transaction():
            cur = store.conn.cursor()
            for row in rows:
                cols = ", ".join(row)
                marks = ", ".join("?" for _ in row)
                cur.execute(store.dialect.sql(
                    f"INSERT INTO {table} ({cols}) VALUES ({marks})"),
                    tuple(row.values()))

    def _delete(self, store, table, row_id):
        with store.transaction():
            store.conn.cursor().execute(store.dialect.sql(
                f"DELETE FROM {table} WHERE id = ?"), (row_id,))

    def _recorded(self, store, version):
        cur = store.conn.cursor()
        cur.execute(store.dialect.sql(
            "SELECT version FROM schema_migrations WHERE version = ?"), (version,))
        return cur.fetchone() is not None

    def _index_exists(self, store, index):
        cur = store.conn.cursor()
        if store.dialect.backend == "postgres":
            cur.execute("SELECT 1 FROM pg_indexes WHERE schemaname = "
                        "current_schema() AND indexname = %s", (index,))
        else:
            cur.execute("SELECT 1 FROM sqlite_master WHERE type = 'index' "
                        "AND name = ?", (index,))
        return cur.fetchone() is not None

    def _assert_halted(self, store, version, label):
        with self.assertRaises(MigrationDataError, msg=label) as ctx:
            migrate(store.conn, store.dialect)
        self.assertFalse(self._recorded(store, version),
                         f"{label}: ledger written despite the halt")
        for index in INDEXES[version]:
            self.assertFalse(self._index_exists(store, index),
                             f"{label}: {index} created despite the halt")
        return str(ctx.exception)

    def _assert_applied(self, store, version, label):
        migrate(store.conn, store.dialect)
        self.assertTrue(self._recorded(store, version), label)
        for index in INDEXES[version]:
            self.assertTrue(self._index_exists(store, index), f"{label}: {index}")

    # -- 047: officials.external_ref ----------------------------------------
    def test_duplicate_official_external_ref_halts_047_and_names_it(self):
        def body(store, label):
            self._insert(store, "officials", [
                {"id": "off_dup_a", "name": "Ref Alpha", "external_ref": "OFF-DUP"},
                {"id": "off_dup_b", "name": "Ref Bravo", "external_ref": "OFF-DUP"},
                {"id": "off_ok", "name": "Ref Charlie", "external_ref": "OFF-OK"},
            ])
            message = self._assert_halted(store, V047, label)
            self.assertIn("OFF-DUP", message, label)
            self.assertIn("1 external_ref value(s)", message, label)
            self.assertNotIn("OFF-OK", message, label)
            self.assertNotIn("tuple(s) already back", message, label)
            self._delete(store, "officials", "off_dup_b")
            self._assert_applied(store, V047, label)
        self._each_backend(V047, body)

    # -- 047: official_availability(official_id, start_time, end_time) ------
    def test_duplicate_availability_tuple_halts_047_with_tuple_and_ids(self):
        def body(store, label):
            self._insert(store, "officials", [
                {"id": "off_win", "name": "Ref Delta", "external_ref": None},
            ])
            start, end = "2026-09-05T18:00:00+00:00", "2026-09-05T20:00:00+00:00"
            self._insert(store, "official_availability", [
                {"id": "win_dup_a", "official_id": "off_win",
                 "start_time": start, "end_time": end, "status": "available"},
                {"id": "win_dup_b", "official_id": "off_win",
                 "start_time": start, "end_time": end, "status": "available"},
                # Same official and start, a different end: not a duplicate.
                {"id": "win_other", "official_id": "off_win",
                 "start_time": start, "end_time": "2026-09-05T21:00:00+00:00",
                 "status": "available"},
            ])
            message = self._assert_halted(store, V047, label)
            self.assertIn(f"official off_win [{start} - {end}]", message, label)
            self.assertIn("windows win_dup_a, win_dup_b", message, label)
            self.assertNotIn("win_other", message, label)
            self.assertNotIn("external_ref value(s)", message, label)
            self._delete(store, "official_availability", "win_dup_b")
            self._assert_applied(store, V047, label)
        self._each_backend(V047, body)

    def test_null_bearing_rows_stay_allowed_by_047(self):
        def body(store, label):
            self._insert(store, "officials", [
                {"id": "off_null_a", "name": "Ref Echo", "external_ref": None},
                {"id": "off_null_b", "name": "Ref Foxtrot", "external_ref": None},
            ])
            start = "2026-09-06T18:00:00+00:00"
            self._insert(store, "official_availability", [
                # Each tuple repeats, but every repeat has a NULL member, so
                # the partial index excludes it and the check must too.
                {"id": "win_null_end_a", "official_id": "off_null_a",
                 "start_time": start, "end_time": None},
                {"id": "win_null_end_b", "official_id": "off_null_a",
                 "start_time": start, "end_time": None},
                {"id": "win_null_off_a", "official_id": None,
                 "start_time": start, "end_time": "2026-09-06T20:00:00+00:00"},
                {"id": "win_null_off_b", "official_id": None,
                 "start_time": start, "end_time": "2026-09-06T20:00:00+00:00"},
            ])
            self._assert_applied(store, V047, label)
        self._each_backend(V047, body)

    # -- 048: rinks.external_ref --------------------------------------------
    def test_duplicate_rink_external_ref_halts_048_and_names_it(self):
        def body(store, label):
            self._insert(store, "rinks", [
                {"id": "rink_dup_a", "name": "North Ice", "external_ref": "RINK-DUP"},
                {"id": "rink_dup_b", "name": "South Ice", "external_ref": "RINK-DUP"},
                {"id": "rink_ok", "name": "East Ice", "external_ref": "RINK-OK"},
            ])
            message = self._assert_halted(store, V048, label)
            self.assertIn("RINK-DUP", message, label)
            self.assertIn("1 rink_code value(s)", message, label)
            self.assertNotIn("RINK-OK", message, label)
            self._delete(store, "rinks", "rink_dup_b")
            self._assert_applied(store, V048, label)
        self._each_backend(V048, body)

    def test_null_external_ref_rinks_stay_allowed_by_048(self):
        def body(store, label):
            self._insert(store, "rinks", [
                {"id": "rink_null_a", "name": "Practice A", "external_ref": None},
                {"id": "rink_null_b", "name": "Practice B", "external_ref": None},
            ])
            self._assert_applied(store, V048, label)
        self._each_backend(V048, body)


if __name__ == "__main__":
    unittest.main()
