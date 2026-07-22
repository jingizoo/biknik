"""One physical ice slot per (rink, start, end) — DB-enforced (#158 review).

Migration 045's UNIQUE index (``ux_ice_slots_rink_time``) is the atomic backstop
for the Ice Availability Builder's idempotent commit. The TOCTOU this PR closes
could already have let a concurrent writer persist an exact duplicate on a
deployed database, so — like every other constraint migration (#201) — 045
registers a pre-migration data check (``assert_no_duplicate_ice_slot_times``)
that names the offending ``(rink, start, end)`` tuple and slot ids and fails the
upgrade BEFORE the DDL, instead of letting ``CREATE UNIQUE INDEX`` raise an
opaque driver error over dirty data.

Covers, on SQLite + PostgreSQL: duplicate detection/report, an abort that leaves
no index and no ledger row on dirty data, a durable clean upgrade that then
enforces the constraint, that a NULL anywhere in the tuple is never flagged
(the index treats those rows as distinct), and ledger persistence across a
reopen.
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain.errors import IntegrityConflictError
from hockey_scheduler.domain.setup_models import IceSlot, Rink, Venue
from hockey_scheduler.store import SqlStore
from hockey_scheduler.store.integrity_checks import (
    MigrationDataError,
    assert_no_duplicate_ice_slot_times,
    find_duplicate_ice_slot_times,
)
from hockey_scheduler.store.sql_store import migrate

UTC = timezone.utc
_VERSION = "045_ice_slot_unique_time"
_INDEX = "ux_ice_slots_rink_time"


def dt(h):
    return datetime(2026, 9, 1, h, tzinfo=UTC)


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
    """Leave a shared PostgreSQL database CONSTRUCTABLE after a test that
    downgraded migration 045 and planted duplicates: rebuild it to a clean,
    fully migrated schema so the next ``SqlStore(url)`` — whose ``migrate()`` now
    runs this PR's pre-migration duplicate check — doesn't abort over leftovers
    (which would cascade into every later PostgreSQL test). A private ``:memory:``
    database is discarded on close, so only ``close`` is needed there."""
    try:
        if url != ":memory:":
            store.reset_schema()
    finally:
        store.close()


def _downgrade_045(store):
    """Simulate a pre-045 database: drop the index and un-record the migration."""
    with store.transaction():
        cur = store.conn.cursor()
        cur.execute(f"DROP INDEX IF EXISTS {_INDEX}")
        cur.execute(store.dialect.sql(
            "DELETE FROM schema_migrations WHERE version = ?"), (_VERSION,))


def _seed_rink(store):
    """A real rink for the ice_slots.rink_id -> rinks foreign key (migration 041);
    the index only constrains rows whose rink_id is non-null."""
    with store.transaction():
        store.add_venue(Venue(id="v1", name="Arena"))
        store.add_rink(Rink(id="r1", venue_id="v1", name="Rink 1"))


def _slot(sid, rink="r1", start=None, end=None):
    return IceSlot(id=sid, rink_id=rink,
                   start_time=start or dt(18), end_time=end or dt(20))


class PreMigration045ValidationTest(unittest.TestCase):
    """Existing duplicates are detected and named, never guessed away (SQLite)."""

    def test_duplicate_slot_times_detected_and_named(self):
        store = SqlStore(":memory:")
        try:
            _downgrade_045(store)
            _seed_rink(store)
            with store.transaction():
                store.add_ice_slot(_slot("s1"))                        # r1 18-20
                store.add_ice_slot(_slot("s2"))                        # dup tuple
                store.add_ice_slot(_slot("s3", start=dt(20), end=dt(22)))  # distinct
            dupes = find_duplicate_ice_slot_times(store.conn)
            self.assertEqual(len(dupes), 1)
            rink, _start, _end, ids = dupes[0]
            self.assertEqual(rink, "r1")
            self.assertEqual(ids, ["s1", "s2"])       # both offenders, sorted
            with self.assertRaises(MigrationDataError) as ctx:
                assert_no_duplicate_ice_slot_times(store.conn)
            msg = str(ctx.exception)
            for token in ("r1", "s1", "s2"):
                self.assertIn(token, msg)
        finally:
            store.close()

    def test_distinct_and_null_tuples_not_flagged(self):
        store = SqlStore(":memory:")
        try:
            _downgrade_045(store)
            _seed_rink(store)
            with store.transaction():
                store.add_ice_slot(_slot("s1"))
                store.add_ice_slot(_slot("s2", start=dt(20), end=dt(22)))
                # NULL start/end: the index keeps these distinct, so two of them
                # are NOT a violation and must not be reported.
                store.add_ice_slot(IceSlot(id="n1", rink_id="r1",
                                           start_time=None, end_time=None))
                store.add_ice_slot(IceSlot(id="n2", rink_id="r1",
                                           start_time=None, end_time=None))
            self.assertEqual(find_duplicate_ice_slot_times(store.conn), [])
            assert_no_duplicate_ice_slot_times(store.conn)  # does not raise
        finally:
            store.close()


class Migration045UpgradeTest(unittest.TestCase):
    """The upgrade aborts cleanly on dirty data and is durable on clean data."""

    def test_migrate_aborts_on_dirty_data_with_no_partial_state(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                _downgrade_045(store)
                _seed_rink(store)
                with store.transaction():
                    store.add_ice_slot(_slot("s1"))
                    store.add_ice_slot(_slot("s2"))    # dup (r1, 18-20)
                with self.assertRaises(MigrationDataError, msg=label) as ctx:
                    migrate(store.conn, store.dialect)
                self.assertIn("s1", str(ctx.exception), label)
                # No ledger row: the version was never recorded.
                self.assertNotIn(_VERSION,
                                 store.migration_status()["applied"], label)
                # No index: a third duplicate still inserts (the constraint the
                # aborted DDL would have added does not exist).
                with store.transaction():
                    store.add_ice_slot(_slot("s3"))
                self.assertIsNotNone(store.get_ice_slot("s3"), label)
            finally:
                _restore(store, url)

    def test_clean_data_migrates_durably_and_then_enforces(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                _downgrade_045(store)
                _seed_rink(store)
                with store.transaction():
                    store.add_ice_slot(_slot("s1"))                       # 18-20
                    store.add_ice_slot(_slot("s2", start=dt(20), end=dt(22)))
                migrate(store.conn, store.dialect)     # clean -> applies 045
                self.assertIn(_VERSION,
                              store.migration_status()["applied"], label)
                # The index now enforces: a duplicate tuple is the stable
                # translated conflict, not a raw driver error.
                with self.assertRaises(IntegrityConflictError, msg=label) as ctx:
                    with store.transaction():
                        store.add_ice_slot(_slot("dup"))   # dup of s1 (r1, 18-20)
                self.assertEqual(ctx.exception.details["reason"],
                                 "ice_slot_time_taken", label)
            finally:
                _restore(store, url)


class Migration045LedgerTest(unittest.TestCase):
    """The constraint is recorded and still enforces after a restart (SQLite)."""

    def test_recorded_and_enforced_after_reopen(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            first = SqlStore(path)
            self.assertIn(_VERSION, first.migration_status()["applied"])
            _seed_rink(first)
            with first.transaction():
                first.add_ice_slot(_slot("s1"))
            first.close()
            # Reopen: migrate() is idempotent and the index still enforces.
            second = SqlStore(path)
            self.assertIn(_VERSION, second.migration_status()["applied"])
            self.assertTrue(second.migration_status()["current"])
            with self.assertRaises(IntegrityConflictError):
                with second.transaction():
                    second.add_ice_slot(_slot("s2"))   # dup of s1 -> rejected
            second.close()
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
