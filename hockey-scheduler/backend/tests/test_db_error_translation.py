"""Database-exception → domain-error translation (#201 Slice 2).

The store translates recognized driver failures into stable, secret-free domain
errors at one boundary (SqlStore.transaction), after rollback. These tests cover
the required matrix across SQLite and — when TEST_DATABASE_URL is set (CI) —
PostgreSQL, plus deterministic unit tests of the translator for failure shapes
whose enforcement lands in later slices (FK/check) or is driver-specific
(lock/serialization/deadlock).
"""

import os
import sqlite3
import unittest
from datetime import datetime, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain import Role, Team, UserAccount
from hockey_scheduler.domain.errors import (
    ConcurrencyConflictError,
    IntegrityConflictError,
    ValidationError,
)
from hockey_scheduler.store import SqlStore
from hockey_scheduler.store.db_errors import translate_db_exception

UTC = timezone.utc


def _account(uid, username):
    return UserAccount(id=uid, username=username, password_hash="h",
                       role=Role.VIEWER, created_at=datetime(2026, 1, 1, tzinfo=UTC),
                       scope={})


class _FakePgError(Exception):
    """Stand-in for a psycopg error: carries only a SQLSTATE, like the real one."""

    def __init__(self, sqlstate):
        super().__init__("db error")  # deliberately generic; must never leak
        self.sqlstate = sqlstate


def _sql_backends():
    """(label, url) for each SQL backend available in this environment."""
    backends = [("sqlite", ":memory:")]
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        backends.append(("postgres", url))
    return backends


class TranslatorUnitTest(unittest.TestCase):
    """Deterministic mapping of driver failure shapes, no live DB needed."""

    def _assert_no_leak(self, err, *forbidden):
        blob = (err.message + repr(err.details)).lower()
        for token in forbidden:
            self.assertNotIn(token.lower(), blob,
                             f"translated error leaked {token!r}: {blob}")

    # (2) FK/check shapes translate without leaking database details, even
    # though enforcement lands in a later slice.
    def test_foreign_key_shape_translates_without_leaking(self):
        for exc in (sqlite3.IntegrityError(
                        "FOREIGN KEY constraint failed"),
                    _FakePgError("23503")):
            err = translate_db_exception(exc)
            self.assertIsInstance(err, IntegrityConflictError)
            self.assertEqual(err.code, "conflict")
            self.assertEqual(err.details["reason"], "foreign_key_violation")
            self._assert_no_leak(err, "foreign key", "constraint", "sqlstate",
                                 "23503", "db error")

    def test_check_shape_translates_without_leaking(self):
        for exc in (sqlite3.IntegrityError(
                        "CHECK constraint failed: games.status"),
                    _FakePgError("23514")):
            err = translate_db_exception(exc)
            self.assertIsInstance(err, IntegrityConflictError)
            self.assertEqual(err.details["reason"], "check_violation")
            self._assert_no_leak(err, "check constraint", "games", "status",
                                 "23514")

    # (3) lock / deadlock / serialization → retryable concurrency classification.
    def test_concurrency_shapes_translate_to_retryable(self):
        cases = {
            sqlite3.OperationalError("database is locked"): "lock_not_available",
            _FakePgError("40001"): "serialization_failure",
            _FakePgError("40P01"): "deadlock_detected",
            _FakePgError("55P03"): "lock_not_available",
        }
        for exc, reason in cases.items():
            err = translate_db_exception(exc)
            self.assertIsInstance(err, ConcurrencyConflictError)
            self.assertEqual(err.code, "concurrency_conflict")
            self.assertEqual(err.details["reason"], reason)
            self.assertTrue(err.details["retryable"])
            self._assert_no_leak(err, "locked", "sqlstate", "40001", "40p01")

    # (4) an unknown/unclassified database error is NOT translated (stays
    # internal), never a user error.
    def test_unknown_db_exceptions_pass_through(self):
        for exc in (RuntimeError("boom"),
                    sqlite3.OperationalError("no such table: widgets"),
                    _FakePgError("42P01"),        # undefined_table
                    ValidationError("bad input")):  # a real domain error
            self.assertIsNone(translate_db_exception(exc))

    # (6, deterministic half) identical response shape across backends for the
    # same conflict, independent of whether a live Postgres is present.
    def test_unique_response_identical_across_driver_shapes(self):
        sqlite_err = translate_db_exception(
            sqlite3.IntegrityError("UNIQUE constraint failed: user_accounts.username"))
        pg_err = translate_db_exception(_FakePgError("23505"))
        self.assertEqual(sqlite_err.to_dict(), pg_err.to_dict())
        self.assertEqual(sqlite_err.to_dict(),
                         {"error": {"code": "conflict",
                                    "message": "The change conflicts with an existing record.",
                                    "details": {"reason": "unique_violation"}}})


class TranslatorAtStoreBoundaryTest(unittest.TestCase):
    """End-to-end through SqlStore.transaction on each available SQL backend."""

    def _store(self, url):
        store = SqlStore(url)
        if url != ":memory:":
            store.reset_schema()  # isolate from other tests on a shared DB
        return store

    # (1) a real unique-constraint violation → stable domain conflict, and
    # (6, live half) an identical response on every backend.
    def test_unique_violation_becomes_conflict_on_each_backend(self):
        responses = {}
        for label, url in _sql_backends():
            store = self._store(url)
            try:
                with store.transaction():
                    store.add_user_account(_account("user_1", "dup"))
                with self.assertRaises(IntegrityConflictError, msg=label) as ctx:
                    with store.transaction():
                        store.add_user_account(_account("user_2", "dup"))
                self.assertEqual(ctx.exception.details["reason"],
                                 "unique_violation", label)
                responses[label] = ctx.exception.to_dict()
            finally:
                store.close()
        # (6) same business conflict → byte-identical API payload everywhere.
        self.assertEqual(len(set(map(repr, responses.values()))), 1, responses)

    # (5) a translated failure leaves zero entity writes: the earlier write in
    # the same transaction rolls back too.
    def test_translated_conflict_makes_zero_writes_on_each_backend(self):
        for label, url in _sql_backends():
            store = self._store(url)
            try:
                with store.transaction():
                    store.add_user_account(_account("user_1", "dup"))
                teams_before = len(store.all_teams())
                with self.assertRaises(IntegrityConflictError, msg=label):
                    with store.transaction():
                        store.add_team(Team(id="team_x", name="Rolled Back",
                                            league_id=None, club_id=None))
                        store.add_user_account(_account("user_2", "dup"))
                self.assertEqual(len(store.all_teams()), teams_before, label)
                self.assertIsNone(store.get_team("team_x"), label)
            finally:
                store.close()

    # (4, end-to-end) an unknown error inside a transaction propagates unchanged.
    def test_unknown_error_propagates_uninterpreted(self):
        store = SqlStore(":memory:")
        try:
            with self.assertRaises(RuntimeError):
                with store.transaction():
                    store.add_team(Team(id="t1", name="T", league_id=None,
                                        club_id=None))
                    raise RuntimeError("not a database failure")
            # And it rolled back — no partial write survived.
            self.assertIsNone(store.get_team("t1"))
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
