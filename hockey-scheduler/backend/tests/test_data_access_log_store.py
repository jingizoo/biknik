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
from hockey_scheduler.domain.errors import IntegrityConflictError, ValidationError
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

    # -- #426 review finding 5: ordering / integrity / immutability --------
    def test_chronological_order_survives_a_double_digit_row_count(self):
        # The bug this proves fixed: SQL's OLD `ORDER BY id` sorted the
        # TEXTUAL "daccess_<n>" label lexicographically, so row 10 sorted
        # before row 2. `seq` is a real integer, assigned in insertion
        # order, so >9 rows must list back in the SAME order they were
        # inserted regardless of backend.
        ids = []
        for n in range(1, 12):  # 11 rows: crosses the 9->10 digit boundary
            row = _row(self.store, n, subject_id=f"official:o{n}")
            self.store.add_data_access(row)
            ids.append(row.id)
        listed = [r.id for r in self.store.list_data_access()]
        self.assertEqual(listed, ids)
        seqs = [r.seq for r in self.store.list_data_access()]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(set(seqs)), len(seqs))  # no collisions

    def test_seq_is_stable_across_a_restart(self):
        # Chronological order must survive a close/reopen, not merely hold
        # for the life of one open connection/process.
        for n in range(1, 4):
            self.store.add_data_access(
                _row(self.store, n, subject_id=f"official:o{n}"))
        before = [(r.id, r.seq) for r in self.store.list_data_access()]
        reopened = self._reopen_same_backend()
        if reopened is None:
            self.skipTest("no reopen for this backend")
        try:
            after = [(r.id, r.seq) for r in reopened.list_data_access()]
        finally:
            if reopened is not self.store:
                reopened.close()
        self.assertEqual(before, after)

    def _reopen_same_backend(self):
        return None  # overridden by SQL subclasses; Memory has no restart

    def test_duplicate_id_is_refused_not_silently_accepted(self):
        first = _row(self.store, 1)
        self.store.add_data_access(first)
        dup = _row(self.store, 2)
        dup.id = first.id  # same id, otherwise distinct content
        with self.assertRaises(IntegrityConflictError):
            self.store.add_data_access(dup)
        # The rejected duplicate left no trace and did not corrupt the
        # original row.
        self.assertEqual(len(self.store.list_data_access()), 1)
        self.assertEqual(self.store.list_data_access()[0], first)

    def test_invalid_outcome_is_refused(self):
        row = _row(self.store, 1)
        row.outcome = "maybe"
        with self.assertRaises((ValidationError, IntegrityConflictError)):
            self.store.add_data_access(row)
        self.assertEqual(self.store.list_data_access(), [])

    def test_invalid_category_is_refused(self):
        row = _row(self.store, 1)
        row.category = "ssn"  # not in SensitiveFieldCategory
        with self.assertRaises((ValidationError, IntegrityConflictError)):
            self.store.add_data_access(row)
        self.assertEqual(self.store.list_data_access(), [])

    def test_listed_rows_are_immutable_snapshots(self):
        # #426 review finding 5: "Memory also returns live mutable audit
        # objects... unlike SQL" — mutating a row a caller received back
        # must never corrupt the durable record.
        self.store.add_data_access(_row(self.store, 1))
        got = self.store.list_data_access()[0]
        got.outcome = ACCESS_DENIED
        got.actor_role = "tampered"
        still = self.store.list_data_access()[0]
        self.assertEqual(still.outcome, ACCESS_ALLOWED)
        self.assertEqual(still.actor_role, "league_admin")

    # -- #426 round-2 review finding 4: the WRITE api's own caller-mutable ---
    # object identity — "InMemoryStore.add_data_access() stores and returns
    # the exact caller-owned DataAccessLog object; changing the original
    # object or the returned value after insertion changes the durable
    # row". Distinct from test_listed_rows_are_immutable_snapshots above,
    # which only pins list_data_access()'s OWN read-side copying; these
    # pin add_data_access()/add_data_access_durable()'s WRITE-side identity
    # instead — proven on all three backends, so the SQL-parity claim
    # itself ("still differs from SQL") is asserted, not merely assumed.

    def test_add_original_object_not_mutable_after_insert(self):
        row = _row(self.store, 1)
        self.store.add_data_access(row)
        row.outcome = ACCESS_DENIED
        row.actor_role = "tampered"
        stored = self.store.list_data_access()[0]
        self.assertEqual(stored.outcome, ACCESS_ALLOWED)
        self.assertEqual(stored.actor_role, "league_admin")

    def test_add_return_value_not_mutable_after_insert(self):
        row = _row(self.store, 1)
        returned = self.store.add_data_access(row)
        returned.outcome = ACCESS_DENIED
        returned.actor_role = "tampered"
        stored = self.store.list_data_access()[0]
        self.assertEqual(stored.outcome, ACCESS_ALLOWED)
        self.assertEqual(stored.actor_role, "league_admin")

    def test_durable_add_not_nested_original_object_not_mutable(self):
        row = _row(self.store, 1)
        self.store.add_data_access_durable(row)
        row.outcome = ACCESS_DENIED
        stored = self.store.list_data_access()[0]
        self.assertEqual(stored.outcome, ACCESS_ALLOWED)

    def test_durable_add_not_nested_return_value_not_mutable(self):
        row = _row(self.store, 1)
        returned = self.store.add_data_access_durable(row)
        returned.outcome = ACCESS_DENIED
        stored = self.store.list_data_access()[0]
        self.assertEqual(stored.outcome, ACCESS_ALLOWED)

    def test_durable_add_nested_original_object_immune_before_and_after_flush(self):
        # Nested (queued) durable write, ambient transaction COMMITS: the
        # original object is mutated once WHILE STILL QUEUED (before the
        # flush its own transaction triggers on exit) and once more AFTER
        # the flush — neither mutation may reach the persisted row.
        row = _row(self.store, 1)
        with self.store.transaction():
            self.store.add_data_access_durable(row)
            row.outcome = ACCESS_DENIED  # mutate pre-flush, still nested
        stored = self.store.list_data_access()[0]
        self.assertEqual(stored.outcome, ACCESS_ALLOWED)
        row.actor_role = "tampered-post-flush"  # mutate again, post-flush
        stored_again = self.store.list_data_access()[0]
        self.assertEqual(stored_again.actor_role, "league_admin")

    def test_durable_add_nested_return_value_immune_before_and_after_flush(self):
        row = _row(self.store, 1)
        with self.store.transaction():
            returned = self.store.add_data_access_durable(row)
            returned.outcome = ACCESS_DENIED
        stored = self.store.list_data_access()[0]
        self.assertEqual(stored.outcome, ACCESS_ALLOWED)
        returned.actor_role = "tampered-post-flush"
        stored_again = self.store.list_data_access()[0]
        self.assertEqual(stored_again.actor_role, "league_admin")

    def test_durable_add_nested_survives_rollback_immune_to_post_mutation(self):
        # The compound case: a nested durable write inside a transaction
        # that then ROLLS BACK still durably persists (finding 1/4's own
        # core property), and mutating the caller's object OR the return
        # value afterward must not reach the row that survived either.
        class Boom(Exception):
            pass

        row = _row(self.store, 1)
        try:
            with self.store.transaction():
                returned = self.store.add_data_access_durable(row)
                raise Boom()
        except Boom:
            pass
        stored = self.store.list_data_access()
        self.assertEqual(len(stored), 1, stored)
        self.assertEqual(stored[0].outcome, ACCESS_ALLOWED)
        row.outcome = ACCESS_DENIED
        returned.outcome = ACCESS_DENIED
        stored_after = self.store.list_data_access()[0]
        self.assertEqual(stored_after.outcome, ACCESS_ALLOWED)


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

    def _reopen_same_backend(self):
        return SqlStore(self._tmp)

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

    def _reopen_same_backend(self):
        return SqlStore(self.url)

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
