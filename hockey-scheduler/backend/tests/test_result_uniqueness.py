"""One result row per game — DB-enforced invariant (#201 Slice 3C).

A UNIQUE index (migration 024) guarantees a game has at most one result row, so
the service's record-once-then-update rule (setup_service.record_result) holds
even under a cross-process race. Covers pre-migration duplicate detection/report,
the constraint on SQLite + PostgreSQL, the stable translated conflict with zero
writes, a real two-connection race, and restart/migration-ledger persistence.
"""

import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain import GameResult, ResultStatus
from hockey_scheduler.domain.errors import IntegrityConflictError
from hockey_scheduler.store import SqlStore
from hockey_scheduler.store.integrity_checks import (
    MigrationDataError,
    assert_no_duplicate_result_games,
    find_duplicate_result_games,
)
from hockey_scheduler.store.sql_store import migrate

UTC = timezone.utc
_VERSION = "024_one_result_per_game"
_INDEX = "ux_game_result_game"


def _result(rid, game="g1", home=3, away=2):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return GameResult(
        id=rid, game_id=game, home_score=home, away_score=away,
        status=ResultStatus.DRAFT, recorded_by="actor", recorded_at=now)


def _sql_backends():
    backends = [("sqlite", ":memory:")]
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        backends.append(("postgres", url))
    return backends


def _fresh(url):
    store = SqlStore(url)
    if url != ":memory:":
        store.reset_schema()
    return store


def _downgrade_024(store):
    """Simulate a pre-024 database: drop the unique index and un-record it."""
    with store.transaction():
        cur = store.conn.cursor()
        cur.execute(f"DROP INDEX IF EXISTS {_INDEX}")
        cur.execute(store.dialect.sql(
            "DELETE FROM schema_migrations WHERE version = ?"), (_VERSION,))


def _game_lookup_index_present(store):
    """True if a NON-partial ix_game_results_game (usable for result_for_game's
    WHERE game_id = ? scan, including any NULL-game_id rows) exists."""
    cur = store.conn.cursor()
    if store.backend == "sqlite":
        cur.execute("PRAGMA index_list('game_results')")
        return any(row["name"] == "ix_game_results_game" and row["partial"] == 0
                   for row in cur.fetchall())
    cur.execute("SELECT indexname, indexdef FROM pg_indexes "
                "WHERE tablename = 'game_results'")
    return any(row["indexname"] == "ix_game_results_game"
               and "WHERE" not in row["indexdef"].upper()
               for row in cur.fetchall())


def _unique_partial_index_present(store):
    """True if the PARTIAL unique ux_game_result_game (concrete game_id
    uniqueness, NULL-bearing rows excluded) exists."""
    cur = store.conn.cursor()
    if store.backend == "sqlite":
        cur.execute("PRAGMA index_list('game_results')")
        return any(row["name"] == _INDEX and row["unique"] == 1
                   and row["partial"] == 1 for row in cur.fetchall())
    cur.execute("SELECT indexname, indexdef FROM pg_indexes "
                "WHERE tablename = 'game_results'")
    return any(row["indexname"] == _INDEX
               and "UNIQUE" in row["indexdef"].upper()
               and "WHERE" in row["indexdef"].upper()
               for row in cur.fetchall())


class PreMigrationValidationTest(unittest.TestCase):
    """The pre-enable check matches the partial index exactly, on each backend.

    Parametrized over SQLite + PostgreSQL (in CI) so the NULL semantics — which
    differ from a naive GROUP BY — are proven on both, not just SQLite.
    """

    def _pre024(self, url):
        store = _fresh(url)
        _downgrade_024(store)
        return store

    def _cleanup(self, store):
        # These tests write duplicate rows a shared PostgreSQL DB would carry
        # into later suites (whose SqlStore() re-runs migrate() → the 024
        # pre-check would then abort on our leftovers). Reset to a clean,
        # fully-migrated schema before closing so the shared DB is left pristine.
        if store.backend != "sqlite":
            store.reset_schema()
        store.close()

    def test_concrete_duplicates_detected_and_migrate_aborts(self):
        for label, url in _sql_backends():
            store = self._pre024(url)
            try:
                with store.transaction():
                    store.add_game_result(_result("r1", "g1"))
                    store.add_game_result(_result("r2", "g1"))  # dup
                    store.add_game_result(_result("r3", "g2"))
                self.assertEqual(find_duplicate_result_games(store.conn),
                                 ["g1"], label)
                with self.assertRaises(MigrationDataError, msg=label) as ctx:
                    assert_no_duplicate_result_games(store.conn)
                self.assertIn("g1", str(ctx.exception))
                with self.assertRaises(MigrationDataError, msg=label):
                    migrate(store.conn, store.dialect)  # aborts on dirty data
            finally:
                self._cleanup(store)

    def test_null_bearing_duplicates_are_not_flagged_and_do_not_crash(self):
        # game_id is nullable and NULLs are distinct in the partial index, so
        # repeated NULL-bearing rows are NOT duplicates. The check must agree
        # (no false positive) and the migration must still apply.
        for label, url in _sql_backends():
            store = self._pre024(url)
            try:
                with store.transaction():
                    store.add_game_result(_result("r1", None))
                    store.add_game_result(_result("r2", None))
                self.assertEqual(find_duplicate_result_games(store.conn), [], label)
                assert_no_duplicate_result_games(store.conn)  # must not raise
                migrate(store.conn, store.dialect)  # succeeds; index is created
                self.assertIn(_VERSION, store.migration_status()["applied"], label)
            finally:
                self._cleanup(store)

    def test_mixed_null_and_concrete_reports_only_the_concrete_game(self):
        # A dirty mix — (NULL) alongside two ('g1') — must report only the
        # concrete duplicate game.
        for label, url in _sql_backends():
            store = self._pre024(url)
            try:
                with store.transaction():
                    store.add_game_result(_result("r1", None))
                    store.add_game_result(_result("r2", "g1"))
                    store.add_game_result(_result("r3", "g1"))
                self.assertEqual(find_duplicate_result_games(store.conn),
                                 ["g1"], label)
                with self.assertRaises(MigrationDataError, msg=label):
                    assert_no_duplicate_result_games(store.conn)
            finally:
                self._cleanup(store)


class ConstraintEnforcementTest(unittest.TestCase):
    def test_second_result_for_same_game_is_a_translated_conflict(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                with store.transaction():
                    store.add_game_result(_result("r1", "g1"))
                with self.assertRaises(IntegrityConflictError, msg=label) as ctx:
                    with store.transaction():
                        store.add_game_result(_result("r2", "g1"))  # same game
                self.assertEqual(ctx.exception.details["reason"],
                                 "unique_violation", label)
                # A different game is unaffected.
                with store.transaction():
                    store.add_game_result(_result("r3", "g2"))
                self.assertIsNotNone(store.result_for_game("g2"), label)
            finally:
                store.close()

    def test_update_in_place_still_works_under_the_constraint(self):
        # The service updates the single result row (draft → edited → FINAL)
        # rather than inserting a second one; the constraint must not break that.
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                with store.transaction():
                    store.add_game_result(_result("r1", "g1", home=1, away=0))
                result = store.result_for_game("g1")
                result.home_score = 4  # re-enter the score
                with store.transaction():
                    store.save_game_result(result)
                result.status = ResultStatus.FINAL  # approve
                with store.transaction():
                    store.save_game_result(result)
                rows = [r for r in store.all_game_results() if r.game_id == "g1"]
                self.assertEqual(len(rows), 1, label)  # still exactly one row
                self.assertEqual(rows[0].home_score, 4, label)
                self.assertEqual(rows[0].status, ResultStatus.FINAL, label)
            finally:
                store.close()

    def test_null_bearing_rows_are_allowed_by_the_partial_index(self):
        # The index only constrains concrete game_id, so NULL-bearing rows never
        # conflict (matching the pre-migration check).
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                with store.transaction():  # neither of these raises a conflict
                    store.add_game_result(_result("r1", None))
                    store.add_game_result(_result("r2", None))
                cur = store.conn.cursor()
                cur.execute("SELECT COUNT(*) AS n FROM game_results")
                self.assertEqual(cur.fetchone()["n"], 2, label)
            finally:
                store.close()

    def test_conflict_makes_zero_writes(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                with store.transaction():
                    store.add_game_result(_result("r1", "g1"))
                with self.assertRaises(IntegrityConflictError, msg=label):
                    with store.transaction():
                        store.add_game_result(_result("r3", "g9"))  # ok write
                        store.add_game_result(_result("r2", "g1"))  # violates
                self.assertIsNone(store.result_for_game("g9"), label)
            finally:
                store.close()


class ResultRaceTest(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                         "cross-connection race needs PostgreSQL")
    def test_only_one_of_two_racing_inserts_wins(self):
        url = os.environ["TEST_DATABASE_URL"]
        SqlStore(url).reset_schema()
        barrier = threading.Barrier(2)
        results = {}

        def attempt(rid):
            store = SqlStore(url)
            try:
                barrier.wait(timeout=5)
                with store.transaction():
                    store.add_game_result(_result(rid, "race_g"))
                results[rid] = "won"
            except IntegrityConflictError:
                results[rid] = "conflict"
            except Exception as exc:  # pragma: no cover
                results[rid] = f"error:{exc!r}"
            finally:
                store.close()

        threads = [threading.Thread(target=attempt, args=(r,))
                   for r in ("res_a", "res_b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(sorted(results.values()), ["conflict", "won"], results)
        checker = SqlStore(url)
        try:
            cur = checker.conn.cursor()
            cur.execute(checker.dialect.sql(
                "SELECT COUNT(*) AS n FROM game_results WHERE game_id = ?"),
                ("race_g",))
            self.assertEqual(cur.fetchone()["n"], 1)
        finally:
            checker.close()


class IndexShapeTest(unittest.TestCase):
    """After migration 024 both indexes must coexist on every backend: the
    PARTIAL unique index that enforces one row per concrete game, and a
    NON-partial ix_game_results_game that still serves result_for_game's
    ``WHERE game_id = ?`` scan (including any NULL-game_id rows the partial
    index excludes). Dropping the latter for the former would silently
    full-scan.
    """

    def test_partial_unique_and_nonpartial_lookup_indexes_coexist(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                with store.transaction():
                    store.add_game_result(_result("r1", "g1"))
                self.assertTrue(_game_lookup_index_present(store),
                                f"{label}: non-partial ix_game_results_game missing")
                self.assertTrue(_unique_partial_index_present(store),
                                f"{label}: partial ux_game_result_game missing")
                self.assertIsNotNone(store.result_for_game("g1"), label)
                with self.assertRaises(IntegrityConflictError, msg=label):
                    with store.transaction():
                        store.add_game_result(_result("r2", "g1"))
            finally:
                store.close()


class MigrationLedgerTest(unittest.TestCase):
    def test_recorded_and_enforced_after_reopen(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            first = SqlStore(path)
            self.assertIn(_VERSION, first.migration_status()["applied"])
            self.assertTrue(_game_lookup_index_present(first))
            self.assertTrue(_unique_partial_index_present(first))
            with first.transaction():
                first.add_game_result(_result("r1", "g1"))
            first.close()
            second = SqlStore(path)
            self.assertTrue(second.migration_status()["current"])
            # Both index shapes survive the restart, not just the unique one.
            self.assertTrue(_game_lookup_index_present(second))
            self.assertTrue(_unique_partial_index_present(second))
            with self.assertRaises(IntegrityConflictError):
                with second.transaction():
                    second.add_game_result(_result("r2", "g1"))  # still enforced
            second.close()
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
