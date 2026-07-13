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


def _ensure_games(store, *game_ids):
    """Seed the parent games a result references (migration 025 adds a
    game_results.game_id → games(id) foreign key, so a concrete game_id must
    name a real game). NULL game_ids need no parent and are skipped."""
    cur = store.conn.cursor()
    for gid in game_ids:
        if gid is None:
            continue
        cur.execute(store.dialect.sql("SELECT 1 FROM games WHERE id = ?"), (gid,))
        if cur.fetchone() is None:
            cur.execute(store.dialect.sql("INSERT INTO games (id) VALUES (?)"), (gid,))


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


class PreMigrationValidationTest(unittest.TestCase):
    """024's duplicate-detection check aborts an upgrade that already has two
    results for the same game, on each backend.

    (game_id became NOT NULL in Slice 3E / migration 026, so the NULL-semantics
    cases the 024 partial index once needed are covered by test_result_game_not_null
    now — a fully-migrated database can no longer hold a NULL-game result row.)
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
                _ensure_games(store, "g1", "g2")
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


class ConstraintEnforcementTest(unittest.TestCase):
    def test_second_result_for_same_game_is_a_translated_conflict(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                _ensure_games(store, "g1", "g2")
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
                _ensure_games(store, "g1")
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

    def test_conflict_makes_zero_writes(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                _ensure_games(store, "g1", "g9")
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
        seed = SqlStore(url)
        seed.reset_schema()
        _ensure_games(seed, "race_g")  # FK parent for the racing results
        seed.close()
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


class MigrationLedgerTest(unittest.TestCase):
    def test_recorded_and_enforced_after_reopen(self):
        # (The finalized single-plain-index shape and its survival across a
        # restart are asserted in test_result_game_not_null; here we just prove
        # one-result-per-game stays enforced after reopening the database.)
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            first = SqlStore(path)
            self.assertIn(_VERSION, first.migration_status()["applied"])
            _ensure_games(first, "g1")
            with first.transaction():
                first.add_game_result(_result("r1", "g1"))
            first.close()
            second = SqlStore(path)
            self.assertTrue(second.migration_status()["current"])
            with self.assertRaises(IntegrityConflictError):
                with second.transaction():
                    second.add_game_result(_result("r2", "g1"))  # still enforced
            second.close()
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
