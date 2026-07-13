"""Require a game for every result — relationship completion (#201 Slice 3E).

Migration 026 finishes the result → game relationship Slice 3D (#229) started:
game_results.game_id becomes NOT NULL, and — since a required column makes the
Slice 3C partial predicate vacuous and the separate lookup index redundant — the
partial-unique and non-unique lookup indexes are replaced by a single plain
UNIQUE index on game_id (which both enforces one-result-per-game and serves the
per-game read).

Covers pre-migration NULL detection/report + upgrade abort, NOT NULL and the
retained FK enforced as translated conflicts, one-result-per-game still enforced,
valid create + update-in-place, the finalized single-plain-index shape, and
upgrade/rollback/restart — on SQLite + PostgreSQL.
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain import GameResult, ResultStatus
from hockey_scheduler.domain.errors import IntegrityConflictError
from hockey_scheduler.store import SqlStore
from hockey_scheduler.store.integrity_checks import (
    MigrationDataError,
    assert_results_have_game,
    find_results_missing_game,
)
from hockey_scheduler.store.sql_store import migrate

UTC = timezone.utc
_VERSION = "026_result_game_not_null"
_INDEX = "ux_game_result_game"
_LOOKUP = "ix_game_results_game"


def _result(rid, game="g1"):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return GameResult(
        id=rid, game_id=game, home_score=3, away_score=2,
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


def _add_game(store, gid):
    cur = store.conn.cursor()
    cur.execute(store.dialect.sql("INSERT INTO games (id) VALUES (?)"), (gid,))


def _add_result_raw(store, rid, game_id):
    now = datetime(2026, 1, 1, tzinfo=UTC).isoformat()
    cur = store.conn.cursor()
    cur.execute(store.dialect.sql(
        "INSERT INTO game_results (id, game_id, home_score, away_score, status, "
        "recorded_by, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)"),
        (rid, game_id, 3, 2, "draft", "actor", now))


def _downgrade_026(store):
    """Undo migration 026 → the pre-026 (Slice 3D) state: nullable game_id with
    the FK, the partial-unique index and the non-unique lookup index, and 026
    un-recorded. Postgres drops the NOT NULL and swaps the indexes; SQLite rebuilds
    the table nullable (FK checks deferred to COMMIT, by which point every copied
    row — all currently non-null — still references a real game)."""
    with store.transaction():
        cur = store.conn.cursor()
        if store.backend == "postgres":
            cur.execute("ALTER TABLE game_results ALTER COLUMN game_id DROP NOT NULL")
            cur.execute(f"DROP INDEX IF EXISTS {_INDEX}")
            cur.execute(f"CREATE UNIQUE INDEX {_INDEX} ON game_results (game_id) "
                        "WHERE game_id IS NOT NULL")
            cur.execute(f"CREATE INDEX IF NOT EXISTS {_LOOKUP} "
                        "ON game_results (game_id)")
        else:
            cur.execute("PRAGMA defer_foreign_keys = ON")
            cur.execute(
                "CREATE TABLE game_results_p25 (id TEXT PRIMARY KEY, "
                "game_id TEXT REFERENCES games (id), home_score INTEGER, "
                "away_score INTEGER, status TEXT, recorded_by TEXT, "
                "recorded_at TEXT, approved_by TEXT, approved_at TEXT)")
            cur.execute(
                "INSERT INTO game_results_p25 SELECT id, game_id, home_score, "
                "away_score, status, recorded_by, recorded_at, approved_by, "
                "approved_at FROM game_results")
            cur.execute("DROP TABLE game_results")
            cur.execute("ALTER TABLE game_results_p25 RENAME TO game_results")
            cur.execute(f"CREATE INDEX IF NOT EXISTS {_LOOKUP} "
                        "ON game_results (game_id)")
            cur.execute(f"CREATE UNIQUE INDEX {_INDEX} ON game_results (game_id) "
                        "WHERE game_id IS NOT NULL")
        cur.execute(store.dialect.sql(
            "DELETE FROM schema_migrations WHERE version = ?"), (_VERSION,))


def _result_indexes(store):
    """{index_name: (is_unique, is_partial)} for game_results (excluding the
    implicit primary-key autoindex)."""
    cur = store.conn.cursor()
    if store.backend == "sqlite":
        cur.execute("PRAGMA index_list('game_results')")
        return {row["name"]: (bool(row["unique"]), bool(row["partial"]))
                for row in cur.fetchall()
                if not row["name"].startswith("sqlite_autoindex")}
    cur.execute("SELECT indexname, indexdef FROM pg_indexes "
                "WHERE tablename = 'game_results'")
    out = {}
    for row in cur.fetchall():
        d = row["indexdef"].upper()
        out[row["indexname"]] = ("UNIQUE" in d, "WHERE" in d)
    return out


class PreMigrationValidationTest(unittest.TestCase):
    def _pre026(self, url):
        store = _fresh(url)
        _downgrade_026(store)
        return store

    def _cleanup(self, store):
        if store.backend != "sqlite":
            store.reset_schema()  # leave the shared PostgreSQL DB pristine
        store.close()

    def test_null_rows_abort_migration_with_ids_reported(self):
        for label, url in _sql_backends():
            store = self._pre026(url)
            try:
                _add_game(store, "g1")
                with store.transaction():
                    _add_result_raw(store, "r_ok", "g1")     # valid
                    _add_result_raw(store, "r_null_a", None)  # missing game
                    _add_result_raw(store, "r_null_b", None)  # missing game
                self.assertEqual(find_results_missing_game(store.conn),
                                 ["r_null_a", "r_null_b"], label)
                with self.assertRaises(MigrationDataError, msg=label) as ctx:
                    assert_results_have_game(store.conn)
                self.assertIn("r_null_a", str(ctx.exception))
                self.assertIn("r_null_b", str(ctx.exception))
                with self.assertRaises(MigrationDataError, msg=label):
                    migrate(store.conn, store.dialect)  # aborts on NULL rows
            finally:
                self._cleanup(store)

    def test_all_valid_rows_upgrade_cleanly(self):
        for label, url in _sql_backends():
            store = self._pre026(url)
            try:
                _add_game(store, "g1")
                with store.transaction():
                    _add_result_raw(store, "r1", "g1")  # no NULL rows
                self.assertEqual(find_results_missing_game(store.conn), [], label)
                assert_results_have_game(store.conn)  # must not raise
                migrate(store.conn, store.dialect)    # succeeds; column required
                self.assertIn(_VERSION, store.migration_status()["applied"], label)
                with self.assertRaises(IntegrityConflictError, msg=label):
                    with store.transaction():
                        store.add_game_result(_result("r2", None))  # now rejected
            finally:
                self._cleanup(store)


class EnforcementTest(unittest.TestCase):
    def test_null_game_id_is_a_not_null_violation(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                with self.assertRaises(IntegrityConflictError, msg=label) as ctx:
                    with store.transaction():
                        store.add_game_result(_result("r1", None))
                self.assertEqual(ctx.exception.details["reason"],
                                 "not_null_violation", label)
            finally:
                store.close()

    def test_missing_game_is_still_a_foreign_key_violation(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                with self.assertRaises(IntegrityConflictError, msg=label) as ctx:
                    with store.transaction():
                        store.add_game_result(_result("r1", "ghost"))
                self.assertEqual(ctx.exception.details["reason"],
                                 "foreign_key_violation", label)
            finally:
                store.close()

    def test_valid_create_and_update_in_place(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                _add_game(store, "g1")
                with store.transaction():
                    store.add_game_result(_result("r1", "g1"))
                result = store.result_for_game("g1")
                result.home_score = 5           # re-enter the score
                with store.transaction():
                    store.save_game_result(result)
                result.status = ResultStatus.FINAL  # approve
                with store.transaction():
                    store.save_game_result(result)
                rows = [r for r in store.all_game_results() if r.game_id == "g1"]
                self.assertEqual(len(rows), 1, label)
                self.assertEqual(rows[0].home_score, 5, label)
                self.assertEqual(rows[0].status, ResultStatus.FINAL, label)
            finally:
                store.close()

    def test_one_result_per_game_still_enforced(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                _add_game(store, "g1")
                with store.transaction():
                    store.add_game_result(_result("r1", "g1"))
                with self.assertRaises(IntegrityConflictError, msg=label) as ctx:
                    with store.transaction():
                        store.add_game_result(_result("r2", "g1"))
                self.assertEqual(ctx.exception.details["reason"],
                                 "unique_violation", label)
            finally:
                store.close()


class IndexShapeTest(unittest.TestCase):
    """After 026 the result table carries exactly one plain UNIQUE index on
    game_id — the partial-unique and non-unique lookup indexes are gone."""

    def test_single_plain_unique_index(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                indexes = _result_indexes(store)
                self.assertIn(_INDEX, indexes, f"{label}: {indexes}")
                is_unique, is_partial = indexes[_INDEX]
                self.assertTrue(is_unique, label)
                self.assertFalse(is_partial, f"{label}: index still partial")
                self.assertNotIn(_LOOKUP, indexes,
                                 f"{label}: redundant lookup index still present")
                # It still serves the per-game read.
                _add_game(store, "g1")
                with store.transaction():
                    store.add_game_result(_result("r1", "g1"))
                self.assertIsNotNone(store.result_for_game("g1"), label)
            finally:
                store.close()


class RollbackTest(unittest.TestCase):
    """Downgrading to the pre-026 shape and re-upgrading round-trips: the column
    becomes required again and the single plain unique index is restored."""

    def test_downgrade_then_reupgrade_restores_the_final_shape(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                _add_game(store, "g1")
                with store.transaction():
                    store.add_game_result(_result("r1", "g1"))
                _downgrade_026(store)
                self.assertNotIn(_VERSION, store.migration_status()["applied"], label)
                # Pre-026 shape allows a NULL-game row again.
                _add_result_raw(store, "r_null", None)
                # Clear it so the re-upgrade's NOT NULL check can pass, then migrate.
                cur = store.conn.cursor()
                cur.execute("DELETE FROM game_results WHERE id = 'r_null'")
                migrate(store.conn, store.dialect)
                self.assertIn(_VERSION, store.migration_status()["applied"], label)
                indexes = _result_indexes(store)
                self.assertEqual(indexes.get(_INDEX), (True, False), label)
                self.assertNotIn(_LOOKUP, indexes, label)
                self.assertEqual(store.result_for_game("g1").home_score, 3, label)
            finally:
                store.close()


class MigrationLedgerTest(unittest.TestCase):
    def test_recorded_and_enforced_after_reopen(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            first = SqlStore(path)
            self.assertIn(_VERSION, first.migration_status()["applied"])
            self.assertEqual(_result_indexes(first).get(_INDEX), (True, False))
            _add_game(first, "g1")
            with first.transaction():
                first.add_game_result(_result("r1", "g1"))
            first.close()
            second = SqlStore(path)
            self.assertTrue(second.migration_status()["current"])
            self.assertEqual(_result_indexes(second).get(_INDEX), (True, False))
            self.assertNotIn(_LOOKUP, _result_indexes(second))
            with self.assertRaises(IntegrityConflictError):  # NOT NULL still holds
                with second.transaction():
                    second.add_game_result(_result("r2", None))
            with self.assertRaises(IntegrityConflictError):  # uniqueness still holds
                with second.transaction():
                    second.add_game_result(_result("r3", "g1"))
            second.close()
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
