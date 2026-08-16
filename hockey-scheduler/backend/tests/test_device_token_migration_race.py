"""#426 round-6 review finding 2: a writer could race between migration
055's pre-check and its DDL, recreating the raw PostgreSQL-key leak
round-5 had just closed for the ORDINARY (no-race) case.

``migrate()`` used to run ``assert_no_duplicate_device_tokens`` BEFORE
``_apply_migration()`` ever opened a transaction (sql_store.py, the old
``check(conn)`` call sat entirely outside any lock). The review reproduced
the exact window with two real PostgreSQL connections: connection A's
duplicate check returned zero rows; connection B then committed a second
``(recipient_ref, token)`` row; connection A's later ``CREATE UNIQUE
INDEX`` discovered it and PostgreSQL raised a raw ``UniqueViolation`` whose
``DETAIL`` text named the conflicting pair verbatim -- a live push
credential, straight into whatever captures a startup traceback. It also
defeated the round-5 diagnostic entirely: that fix only changed WHAT the
guard is allowed to say, never WHEN it runs relative to the DDL.

THE FIX (``store/sql_store.py``): migration 055 is no longer in the plain
``_PRE_MIGRATION_CHECKS`` dict at all. It lives in the new
``_ATOMIC_PRE_MIGRATION_CHECKS`` dict instead, whose check function runs
INSIDE ``_apply_migration``'s own transaction, AFTER a lock excluding
concurrent writers on the named table has been taken (``LOCK TABLE ... IN
SHARE ROW EXCLUSIVE MODE`` on PostgreSQL; SQLite gets the same guarantee
for free from ``BEGIN IMMEDIATE``'s existing RESERVED lock, since SQLite
allows only one writer at a time). No writer can land a fresh duplicate in
the window between "check passed" and "DDL applied", because that whole
window is now inside one lock. As a defensive backstop UNDERNEATH that
lock, ``_sanitize_atomic_check_ddl_exception`` catches any raw
unique-violation that still somehow reaches this migration's own DDL and
re-raises it as a sanitized ``MigrationDataError`` instead.

Covers, on SQLite + PostgreSQL:

* :class:`AtomicCheckDdlTranslationUnitTest` -- deterministic, no race
  needed: monkeypatches the registered check function to a no-op while a
  REAL duplicate already sits in the table, forcing execution past the
  (bypassed) check into the DDL itself, and confirms the resulting raw
  driver unique-violation is caught and re-raised as a sanitized
  ``MigrationDataError`` that never contains the secret token -- proving
  the belt-and-suspenders translation layer works on its own merits,
  independent of whether the lock ever actually gets exercised.
* :class:`MigrationLockBarrierTest` -- the review's own two-connection
  window, made INTO a deterministic barrier via ``_atomic_check_lock_hook``
  (a test-only seam ``_apply_migration`` calls right after the lock is
  acquired and the check has passed, i.e. exactly "after validation and
  before DDL"): connection A's ``migrate()`` call pauses there; connection
  B's raw attempt to insert the second row of the pair is PROVEN to
  genuinely block (still alive after a real wait, not merely slow) rather
  than land instantly; resuming A lets it complete migration 055 with NO
  exception at all (the duplicate never existed from A's point of view --
  B was still blocked when A's own check ran); B's now-unblocked insert is
  then rejected by the freshly-created unique index, leaving exactly one
  row for the pair. A's own exception-free path is asserted directly; B's
  own raw, unsanitized exception (an artifact of this harness using bare
  SQL to stand in for an arbitrary writer, exactly as the review's own
  reproduction did) is caught inside the racer thread and never printed or
  matched against for message content anywhere this test's assertions
  would echo it into failure output -- only whether the FINAL row count is
  correct is checked, so this test's own stdout/stderr can never carry the
  secret regardless of what B's private exception object contains.
"""

import os
import tempfile
import threading
import unittest
from unittest import mock

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.store import SqlStore
from hockey_scheduler.store.db import connect
from hockey_scheduler.store.integrity_checks import (
    MigrationDataError,
    assert_no_duplicate_device_tokens,
)
from hockey_scheduler.store.sql_store import (
    _ATOMIC_PRE_MIGRATION_CHECKS,
    migrate,
)

_VERSION = "055_device_token_unique_key"
_INDEX = "ux_device_tokens_recipient_token"
_LOCK_HOOK = "hockey_scheduler.store.sql_store._atomic_check_lock_hook"
_CHECKS_PATCH_TARGET = "hockey_scheduler.store.sql_store._ATOMIC_PRE_MIGRATION_CHECKS"

SECRET_TOKEN = "secret-push-token-race-426r6"  # pragma: allowlist secret
RECIPIENT = "official:o1-race-426r6"


def _sql_backends():
    """(label, url) for each SQL backend available here (Postgres in CI)."""
    backends = [("sqlite", ":memory:")]
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        backends.append(("postgres", url))
    return backends


def _fresh(url):
    store = SqlStore(url)
    if url != ":memory:":
        store.reset_schema()  # isolate on a shared DB
    return store


def _restore(store, url):
    try:
        if url != ":memory:":
            store.reset_schema()
    finally:
        store.close()


def _downgrade_055(store):
    with store.transaction():
        cur = store.conn.cursor()
        cur.execute(f"DROP INDEX IF EXISTS {_INDEX}")
        cur.execute(store.dialect.sql(
            "DELETE FROM schema_migrations WHERE version = ?"), (_VERSION,))


def _insert_raw(conn, dialect, row_id, recipient, token):
    cur = conn.cursor()
    cur.execute(dialect.sql(
        "INSERT INTO device_tokens (id, recipient_ref, provider, token, "
        "label, active) VALUES (?, ?, ?, ?, ?, ?)"),
        (row_id, recipient, "fcm", token, None, 1))


def _count_pair(conn, dialect, recipient, token):
    cur = conn.cursor()
    cur.execute(dialect.sql(
        "SELECT COUNT(*) AS n FROM device_tokens "
        "WHERE recipient_ref = ? AND token = ?"), (recipient, token))
    row = cur.fetchone()
    return row["n"] if not isinstance(row, (tuple, list)) else row[0]


class AtomicCheckDdlTranslationUnitTest(unittest.TestCase):
    """Deterministic proof the belt-and-suspenders DDL-exception translator
    works, with no race/timing involved at all."""

    def _run(self, url):
        store = _fresh(url)
        try:
            _downgrade_055(store)
            with store.transaction():
                _insert_raw(store.conn, store.dialect, "devtok_race_a",
                           RECIPIENT, SECRET_TOKEN)
                _insert_raw(store.conn, store.dialect, "devtok_race_b",
                           RECIPIENT, SECRET_TOKEN)

            # Bypass the check entirely (a no-op check_fn), forcing
            # execution straight into the DDL against a table that DOES
            # have a real duplicate -- deterministically exercising the
            # translation layer without needing any concurrency at all.
            noop_atomic_checks = {
                _VERSION: (lambda _conn: None, "device_tokens")}
            with mock.patch(_CHECKS_PATCH_TARGET, noop_atomic_checks):
                with self.assertRaises(MigrationDataError) as ctx:
                    migrate(store.conn, store.dialect)
            msg = str(ctx.exception)
            self.assertNotIn(SECRET_TOKEN, msg, msg)
            self.assertNotIn(RECIPIENT, msg, msg)
            # No partial state: the DDL's own transaction rolled back.
            self.assertNotIn(
                _VERSION, store.migration_status()["applied"])
            return msg
        finally:
            _restore(store, url)

    def test_sqlite(self):
        self._run(":memory:")

    @unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                         "PostgreSQL suite only (TEST_DATABASE_URL not set)")
    def test_postgres(self):
        self._run(os.environ["TEST_DATABASE_URL"])

    def test_real_registered_check_fn_untouched(self):
        """Sanity: the module-level dict this test patches really does
        register the real check function under normal import (i.e. this
        test module's mock.patch is patching something real, not a typo)."""
        check_fn, lock_table = _ATOMIC_PRE_MIGRATION_CHECKS[_VERSION]
        self.assertIs(check_fn, assert_no_duplicate_device_tokens)
        self.assertEqual(lock_table, "device_tokens")


class MigrationLockBarrierTest(unittest.TestCase):
    """The review's own two-connection window, as a deterministic barrier:
    a concurrent writer genuinely blocks, migration 055 completes with NO
    exception (the race is prevented, not merely handled), and the writer's
    now-unblocked attempt is correctly rejected -- exactly one row survives."""

    def _run(self, store_a, conn_b, dialect_b, label):
        _downgrade_055(store_a)
        with store_a.transaction():
            _insert_raw(store_a.conn, store_a.dialect, "devtok_race_seed",
                       RECIPIENT, SECRET_TOKEN)

        paused = threading.Event()
        resume = threading.Event()

        def hook(version):
            if version == _VERSION:
                paused.set()
                assert resume.wait(15), "resume never set"

        results = {}
        errors = {}

        def run_migration():
            try:
                with mock.patch(_LOCK_HOOK, side_effect=hook):
                    migrate(store_a.conn, store_a.dialect)
                results["migrate_raised"] = None
            except BaseException as exc:  # noqa: BLE001 - surface, don't hide
                results["migrate_raised"] = exc

        t_a = threading.Thread(target=run_migration)
        t_a.start()
        self.assertTrue(paused.wait(15), f"{label}: A never reached the "
                        "post-validation barrier")

        # Connection B: a raw attempt to write the SECOND row of the pair,
        # exactly the review's own reproduction shape -- started WHILE A
        # is paused holding its writer-excluding lock, past its own
        # (clean) check.
        def run_writer():
            try:
                _insert_raw(conn_b, dialect_b, "devtok_race_writer",
                           RECIPIENT, SECRET_TOKEN)
                errors["writer"] = None
            except BaseException as exc:  # noqa: BLE001
                # Deliberately NOT inspected for message content anywhere
                # below -- see the module docstring. Only its TYPE/occurrence
                # matters here; never printed, never assertIn'd against.
                errors["writer"] = exc

        t_b = threading.Thread(target=run_writer)
        t_b.start()
        # Confirm B is genuinely blocked, not merely slow to start.
        t_b.join(1.5)
        writer_blocked_during_lock = t_b.is_alive()

        resume.set()
        t_a.join(15)
        t_b.join(15)
        self.assertFalse(t_a.is_alive(), f"{label}: migration thread hung")
        self.assertFalse(t_b.is_alive(), f"{label}: writer thread hung")

        self.assertTrue(
            writer_blocked_during_lock,
            f"{label}: the concurrent writer completed while migration 055 "
            "still held its lock -- the writer-excluding lock is not "
            "actually excluding writers")

        # A's OWN path -- the thing finding 2 is about -- must be totally
        # clean: no exception, because B was still blocked when A's own
        # check ran, so A's check legitimately never saw a duplicate.
        self.assertIsNone(
            results["migrate_raised"], f"{label}: {results['migrate_raised']}")
        self.assertIn(_VERSION, store_a.migration_status()["applied"], label)

        # B's OWN raw write, unblocked only once A released its lock, is
        # now correctly rejected by the freshly-created index -- it is NOT
        # asserted to be exception-free (an unsanitized raw write bypassing
        # every API layer is exactly what round-4's upsert_device_token
        # exists to make unnecessary in real application code); what IS
        # asserted is that it did not silently create a second row.
        self.assertIsNotNone(
            errors["writer"], f"{label}: the writer's now-unblocked raw "
            "duplicate insert should have been rejected by the index")

        # Ledger/index state stays atomic: exactly ONE row for the pair,
        # never two (the seed row survives; the writer's own row never
        # landed).
        n = _count_pair(store_a.conn, store_a.dialect, RECIPIENT, SECRET_TOKEN)
        self.assertEqual(n, 1, f"{label}: expected exactly one surviving "
                         f"row for the pair, found {n}")

    def test_sqlite_two_real_file_connections(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            store_a = SqlStore(path)  # migrates to HEAD first (clean)
            conn_b, dialect_b, _ = connect(path)
            try:
                self._run(store_a, conn_b, dialect_b, "sqlite")
            finally:
                conn_b.close()
                store_a.close()
        finally:
            os.remove(path)

    @unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                         "PostgreSQL suite only (TEST_DATABASE_URL not set)")
    def test_postgres_two_real_connections(self):
        url = os.environ["TEST_DATABASE_URL"]
        store_a = _fresh(url)
        conn_b, dialect_b, _ = connect(url)
        try:
            self._run(store_a, conn_b, dialect_b, "postgres")
        finally:
            conn_b.close()
            _restore(store_a, url)


if __name__ == "__main__":
    unittest.main()
