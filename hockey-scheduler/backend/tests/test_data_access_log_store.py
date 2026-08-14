"""DataAccessLog store surface: tri-store parity + migration 053 (#124).

The same assertions run against InMemoryStore, SqlStore on a SQLite temp
file, and SqlStore on PostgreSQL when ``TEST_DATABASE_URL`` is set (CI's
postgres job) — add/list roundtrip, subject/category filters, transaction
atomicity, durability across reopen, and the factory-reset wipe parity with
the other audit surfaces. Plus the schema gates: migration
``053_data_access_log`` is applied and its column set matches the dataclass
exactly, so hand-written DDL cannot drift from the mapper.
"""

import os
import tempfile
import unittest
from dataclasses import fields
from datetime import datetime, timezone

from helpers import BACKEND, fresh_sql_store  # noqa: F401

from hockey_scheduler.domain import (
    ACCESS_ALLOWED,
    ACCESS_DENIED,
    DataAccessLog,
    SensitiveFieldCategory,
)
from hockey_scheduler.store import InMemoryStore, SqlStore

C = SensitiveFieldCategory


def _row(store, n, *, category=C.CONTACT_DESTINATION, subject_type="recipient",
         subject_id="official:o1", outcome=ACCESS_ALLOWED, actor="user_1",
         role="league_admin"):
    return DataAccessLog(
        id=store.next_id("daccess"),
        category=category,
        subject_type=subject_type,
        subject_id=subject_id,
        purpose="list_contact_destinations",
        at=datetime(2026, 3, 1, 12, 0, n, tzinfo=timezone.utc),
        actor_user_id=actor,
        actor_role=role,
        outcome=outcome,
        request_id=f"req_{n}")


class _StoreContract:
    """Mixin with the shared assertions; subclasses provide the store."""

    def test_add_and_list_roundtrip_preserves_every_field(self):
        row = _row(self.store, 1)
        self.store.add_data_access(row)
        listed = self.store.list_data_access()
        self.assertEqual(len(listed), 1)
        got = listed[0]
        self.assertEqual(got, row)  # dataclass equality: every field
        self.assertIs(type(got.category), C)
        self.assertIsNotNone(got.at.tzinfo)  # tz-aware after the roundtrip

    def test_optional_fields_roundtrip_as_none(self):
        row = _row(self.store, 2, actor=None, role=None)
        row.request_id = None
        self.store.add_data_access(row)
        got = self.store.list_data_access()[0]
        self.assertIsNone(got.actor_user_id)
        self.assertIsNone(got.actor_role)
        self.assertIsNone(got.request_id)

    def test_filters_by_subject_and_category(self):
        self.store.add_data_access(_row(self.store, 1, subject_id="official:o1"))
        self.store.add_data_access(_row(self.store, 2, subject_id="player:p1"))
        self.store.add_data_access(
            _row(self.store, 3, category=C.BIRTHDATE, subject_type="player",
                 subject_id="p1"))
        self.assertEqual(len(self.store.list_data_access()), 3)
        self.assertEqual(
            [r.subject_id for r in self.store.list_data_access(
                subject_type="recipient")],
            ["official:o1", "player:p1"])
        self.assertEqual(
            len(self.store.list_data_access(subject_id="player:p1")), 1)
        self.assertEqual(
            len(self.store.list_data_access(category=C.BIRTHDATE)), 1)
        self.assertEqual(
            len(self.store.list_data_access(category=C.DISCIPLINE_NOTE)), 0)
        self.assertEqual(
            len(self.store.list_data_access(subject_type="player",
                                            subject_id="p1",
                                            category=C.BIRTHDATE)), 1)

    def test_denied_rows_are_first_class(self):
        self.store.add_data_access(
            _row(self.store, 1, subject_id="*", outcome=ACCESS_DENIED,
                 role="coach"))
        got = self.store.list_data_access(subject_id="*")[0]
        self.assertEqual(got.outcome, ACCESS_DENIED)
        self.assertEqual(got.actor_role, "coach")

    def test_transaction_rollback_leaves_no_rows(self):
        # The facade emits audit rows inside the transaction of the read they
        # record; a failed transaction must take the rows with it in every
        # store implementation.
        class Boom(Exception):
            pass

        try:
            with self.store.transaction():
                self.store.add_data_access(_row(self.store, 1))
                self.store.add_data_access(_row(self.store, 2))
                raise Boom()
        except Boom:
            pass
        self.assertEqual(self.store.list_data_access(), [])

    def test_transaction_commit_keeps_rows(self):
        with self.store.transaction():
            self.store.add_data_access(_row(self.store, 1))
        self.assertEqual(len(self.store.list_data_access()), 1)

    def test_clear_all_data_wipes_the_log(self):
        # Same wipe behavior as the other audit surfaces (retention rules are
        # #124 block 3, out of this slice).
        self.store.add_data_access(_row(self.store, 1))
        with self.store.transaction():
            self.store.clear_all_data()
        self.assertEqual(self.store.list_data_access(), [])


class MemoryDataAccessTest(_StoreContract, unittest.TestCase):
    def setUp(self):
        self.store = InMemoryStore()


class SqliteDataAccessTest(_StoreContract, unittest.TestCase):
    def setUp(self):
        fd, self._tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.store = SqlStore(self._tmp)

    def tearDown(self):
        self.store.close()
        os.remove(self._tmp)

    def test_rows_survive_reopen(self):
        row = _row(self.store, 1)
        self.store.add_data_access(row)
        self.store.close()
        reopened = SqlStore(self._tmp)
        try:
            self.assertEqual(reopened.list_data_access(), [row])
        finally:
            reopened.close()

    def test_migration_053_is_applied(self):
        status = self.store.migration_status()
        self.assertIn("053_data_access_log", status["applied"])
        self.assertIn("053_data_access_log", status["expected"])
        self.assertTrue(status["current"])

    def test_table_columns_match_the_dataclass_exactly(self):
        # The hand-written migration DDL and the Spec-derived mapper must
        # agree column-for-column, or inserts break on one engine only.
        cur = self.store.conn.execute("PRAGMA table_info(data_access_logs)")
        columns = [r["name"] for r in cur.fetchall()]
        self.assertEqual(columns, [f.name for f in fields(DataAccessLog)])

    def test_no_column_could_carry_the_protected_value(self):
        # Structural value-freedom at the SCHEMA level: no value/destination
        # column and no free-form JSON detail column to smuggle one into.
        cur = self.store.conn.execute("PRAGMA table_info(data_access_logs)")
        columns = {r["name"] for r in cur.fetchall()}
        self.assertFalse(columns & {"value", "destination", "detail",
                                    "payload", "data"})


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL suite only (TEST_DATABASE_URL not set)")
class PostgresDataAccessTest(_StoreContract, unittest.TestCase):
    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        self.store = fresh_sql_store(self.url)

    def tearDown(self):
        self.store.close()

    def test_rows_survive_reopen(self):
        row = _row(self.store, 1)
        self.store.add_data_access(row)
        self.store.close()
        self.store = SqlStore(self.url)  # tearDown closes it
        self.assertEqual(self.store.list_data_access(), [row])

    def test_migration_053_is_applied(self):
        status = self.store.migration_status()
        self.assertIn("053_data_access_log", status["applied"])
        self.assertTrue(status["current"])

    def test_table_columns_match_the_dataclass_exactly(self):
        cur = self.store.conn.cursor()
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'data_access_logs' "
            "ORDER BY ordinal_position")
        columns = [r["column_name"] for r in cur.fetchall()]
        self.assertEqual(columns, [f.name for f in fields(DataAccessLog)])


if __name__ == "__main__":
    unittest.main()
