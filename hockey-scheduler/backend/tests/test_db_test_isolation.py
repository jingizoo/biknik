"""``fresh_sql_store`` must never delete the evidence of a real failure (#369).

The helper exists because ``run_parallel.py`` gives each worker its own
PostgreSQL database but runs that worker's modules SERIALLY against it, and
``test_iceslot_venue_fks`` deliberately plants rows whose parents do not exist
and never cleans up — so the next module's ``SqlStore(url)`` raises
``MigrationDataError`` from ``assert_iceslot_venue_fks_ready`` and cannot even
open the database.

Its first version recovered by catching bare ``Exception`` and dropping the
schema unconditionally. A repo-owner review flagged that as destructive and
non-falsifiable, and the reasoning is worth keeping written down: a migration
regression that fails *only against existing data* would raise on the first
open, have its evidence erased here, and then PASS on the automatic retry
against an empty database. The suite would go green **because** the bug was
real. That is strictly worse than no recovery at all.

Two properties are pinned here, and both are about what the helper must
REFUSE to do:

1. an unexpected exception PROPAGATES, with schema and data intact — proven by
   reading the data back afterwards, since erasure was the actual defect;
2. a database that cannot be PROVEN disposable is never dropped, even when the
   failure is the known poison — proven by name, with no I/O.

Deliberately runs on SQLite so it is meaningful on every CI job, not only the
PostgreSQL one; the Postgres-specific naming rule is asserted through the same
pure-string predicate the helper itself uses.
"""

import os
import sqlite3
import tempfile
import unittest

from helpers import (  # noqa: F401  (BACKEND ensures sys.path is set up)
    BACKEND, _assert_disposable_test_database, fresh_sql_store)

from hockey_scheduler.store import SqlStore
from hockey_scheduler.store.integrity_checks import MigrationDataError


class DisposableDatabaseAssertionTest(unittest.TestCase):
    """The guard that must hold BEFORE any drop. Pure string parsing, so it is
    exercised identically with or without a live database behind it."""

    def test_postgres_requires_the_per_worker_suffix(self):
        # run_parallel.py's own naming: base database + _p<N> per worker.
        _assert_disposable_test_database(
            "postgresql://u:p@localhost:5432/hockey_p0")
        _assert_disposable_test_database(
            "postgresql://u:p@localhost:5432/hockey_p11")

    def test_postgres_without_the_suffix_is_refused(self):
        for url in ("postgresql://u:p@localhost:5432/hockey",
                    "postgresql://u:p@localhost:5432/production",
                    "postgres://u:p@db.internal:5432/hockey_prod"):
            with self.subTest(url=url):
                with self.assertRaises(AssertionError) as caught:
                    _assert_disposable_test_database(url)
                self.assertIn("cannot be proven to be a throwaway",
                              str(caught.exception))

    def test_sqlite_only_memory_or_a_temp_file_is_disposable(self):
        _assert_disposable_test_database(":memory:")
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            _assert_disposable_test_database(path)
        finally:
            os.unlink(path)

    def test_sqlite_outside_the_temp_directory_is_refused(self):
        with self.assertRaises(AssertionError) as caught:
            _assert_disposable_test_database(
                str(BACKEND / "hockey_scheduler" / "real.db"))
        self.assertIn("cannot be proven to be a disposable test database",
                      str(caught.exception))


class UnexpectedFailurePropagatesTest(unittest.TestCase):
    """The core property: an exception the helper does NOT recognise must
    reach the caller, and must leave the database untouched."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        store = SqlStore(self.path)
        store.create_program_row = None          # (unused; keeps lint quiet)
        api_rows = store.conn.cursor()
        api_rows.execute(
            "INSERT INTO clubs (id, name) VALUES ('c_keep', 'Evidence Club')")
        store.conn.commit()
        store.close()

    def tearDown(self):
        if os.path.exists(self.path):
            os.unlink(self.path)

    def _club_names(self):
        conn = sqlite3.connect(self.path)
        try:
            return [r[0] for r in conn.execute("SELECT name FROM clubs")]
        finally:
            conn.close()

    def test_unexpected_exception_propagates_and_preserves_data(self):
        """A migration/preflight failure that is NOT the known poison must not
        be swallowed, and the data that would have proven it must survive."""
        import hockey_scheduler.store as store_pkg

        boom = RuntimeError("simulated migration regression on existing data")
        real_sql_store = store_pkg.SqlStore

        class ExplodingStore(real_sql_store):
            def __init__(self, url, *a, **kw):
                raise boom

        store_pkg.SqlStore = ExplodingStore
        try:
            with self.assertRaises(RuntimeError) as caught:
                fresh_sql_store(self.path)
            self.assertIs(caught.exception, boom,
                          "the original exception must reach the caller "
                          "unchanged, not be re-raised or masked")
        finally:
            store_pkg.SqlStore = real_sql_store

        # The whole point: the evidence is still here.
        self.assertEqual(
            self._club_names(), ["Evidence Club"],
            "an unexpected failure must leave schema and data intact -- "
            "erasing them is what made the original helper non-falsifiable")

    def test_known_poison_is_recovered_through_the_bounded_path(self):
        """The one condition the helper IS allowed to recover from still
        works, on a provably disposable database."""
        import hockey_scheduler.store as store_pkg

        real_sql_store = store_pkg.SqlStore
        state = {"first": True}

        class PoisonedOnce(real_sql_store):
            def __init__(self, url, *a, **kw):
                if state["first"]:
                    state["first"] = False
                    raise MigrationDataError("simulated planted-FK poison")
                super().__init__(url, *a, **kw)

        store_pkg.SqlStore = PoisonedOnce
        try:
            store = fresh_sql_store(self.path)
        finally:
            store_pkg.SqlStore = real_sql_store
        try:
            self.assertEqual(store.all_clubs(), [],
                             "the recovery path returns a CLEAN store")
        finally:
            store.close()

    def test_known_poison_on_a_non_disposable_database_is_refused(self):
        """Even the recognised poison must not authorise dropping a database
        whose name cannot be proven to be a throwaway."""
        import hockey_scheduler.store as store_pkg

        real_sql_store = store_pkg.SqlStore

        class AlwaysPoisoned(real_sql_store):
            def __init__(self, url, *a, **kw):
                raise MigrationDataError("simulated poison")

        store_pkg.SqlStore = AlwaysPoisoned
        try:
            with self.assertRaises(AssertionError) as caught:
                fresh_sql_store("postgresql://u:p@localhost:5432/hockey")
            self.assertIn("cannot be proven to be a throwaway",
                          str(caught.exception))
        finally:
            store_pkg.SqlStore = real_sql_store


if __name__ == "__main__":
    unittest.main()
