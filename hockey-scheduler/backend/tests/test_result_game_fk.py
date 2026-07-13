"""Referential integrity: a result must reference a real game (#201 Slice 3D).

Migration 025 adds a game_results.game_id → games(id) foreign key. This is the
foundation slice for #201's relationship integrity: it turns on SQLite foreign-
key enforcement (PostgreSQL already enforces), adds per-dialect migration support
(SQLite rebuilds the table; PostgreSQL runs an ALTER), and validates existing
data first. The column stays nullable — a nullable FK still permits NULL — so
only a concrete, dangling reference is rejected; requiring the column (NOT NULL)
is a later slice.

Covers pre-migration orphan detection/report + upgrade abort, the FK enforced as
a translated conflict on SQLite + PostgreSQL, NULL still allowed, the SQLite
rebuild preserving rows + indexes, and enforcement surviving a restart.
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
    assert_result_games_exist,
    find_orphan_result_games,
)
from hockey_scheduler.store import sql_store as sql_store_mod
from hockey_scheduler.store.sql_store import migrate

UTC = timezone.utc
_VERSION = "025_result_game_fk"


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
    """Insert a result bypassing the FK, to simulate a pre-025 dangling row.

    Runs with SQLite FK enforcement suspended (Postgres has no FK yet on the
    downgraded schema), so an orphan reference can be planted the way legacy
    data would already carry one before the constraint is added.
    """
    now = datetime(2026, 1, 1, tzinfo=UTC).isoformat()
    cur = store.conn.cursor()
    cur.execute(store.dialect.sql(
        "INSERT INTO game_results (id, game_id, home_score, away_score, status, "
        "recorded_by, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)"),
        (rid, game_id, 3, 2, "draft", "actor", now))


def _downgrade_025(store):
    """Simulate a pre-025 database: drop the FK and un-record the migration.

    Postgres drops the named constraint; SQLite rebuilds the table without the
    foreign key (its enforcement is suspended for the swap). Either way the
    game_results.game_id → games(id) reference is gone and legacy dangling rows
    can be planted before the upgrade re-runs.
    """
    with store.transaction():
        cur = store.conn.cursor()
        if store.backend == "postgres":
            cur.execute("ALTER TABLE game_results "
                        "DROP CONSTRAINT IF EXISTS fk_game_results_game")
        else:
            cur.execute("PRAGMA foreign_keys = OFF")
            cur.execute(
                "CREATE TABLE game_results_old (id TEXT PRIMARY KEY, game_id TEXT, "
                "home_score INTEGER, away_score INTEGER, status TEXT, "
                "recorded_by TEXT, recorded_at TEXT, approved_by TEXT, "
                "approved_at TEXT)")
            cur.execute(
                "INSERT INTO game_results_old SELECT id, game_id, home_score, "
                "away_score, status, recorded_by, recorded_at, approved_by, "
                "approved_at FROM game_results")
            cur.execute("DROP TABLE game_results")
            cur.execute("ALTER TABLE game_results_old RENAME TO game_results")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_game_results_game "
                        "ON game_results (game_id)")
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_game_result_game "
                        "ON game_results (game_id) WHERE game_id IS NOT NULL")
            cur.execute("PRAGMA foreign_keys = ON")
        cur.execute(store.dialect.sql(
            "DELETE FROM schema_migrations WHERE version = ?"), (_VERSION,))


def _fk_present(store):
    """True if the game_results.game_id → games foreign key is declared."""
    cur = store.conn.cursor()
    if store.backend == "sqlite":
        cur.execute("PRAGMA foreign_key_list('game_results')")
        return any(row["table"] == "games" for row in cur.fetchall())
    cur.execute(
        "SELECT 1 FROM information_schema.table_constraints "
        "WHERE table_name = 'game_results' AND constraint_type = 'FOREIGN KEY'")
    return cur.fetchone() is not None


class PreMigrationValidationTest(unittest.TestCase):
    def _pre025(self, url):
        store = _fresh(url)
        _downgrade_025(store)
        return store

    def _cleanup(self, store):
        if store.backend != "sqlite":
            store.reset_schema()  # leave the shared PostgreSQL DB pristine
        store.close()

    def test_orphan_reference_detected_and_migrate_aborts(self):
        for label, url in _sql_backends():
            store = self._pre025(url)
            try:
                _add_game(store, "g1")
                with store.transaction():
                    _add_result_raw(store, "r1", "g1")       # valid
                    _add_result_raw(store, "r2", "ghost")    # dangling
                    _add_result_raw(store, "r3", None)       # NULL — allowed
                self.assertEqual(find_orphan_result_games(store.conn),
                                 ["ghost"], label)
                with self.assertRaises(MigrationDataError, msg=label) as ctx:
                    assert_result_games_exist(store.conn)
                self.assertIn("ghost", str(ctx.exception))
                with self.assertRaises(MigrationDataError, msg=label):
                    migrate(store.conn, store.dialect)  # aborts on dangling data
            finally:
                self._cleanup(store)

    def test_valid_and_null_references_upgrade_cleanly(self):
        for label, url in _sql_backends():
            store = self._pre025(url)
            try:
                _add_game(store, "g1")
                with store.transaction():
                    _add_result_raw(store, "r1", "g1")   # valid
                    _add_result_raw(store, "r2", None)   # NULL — allowed
                self.assertEqual(find_orphan_result_games(store.conn), [], label)
                assert_result_games_exist(store.conn)  # must not raise
                migrate(store.conn, store.dialect)     # succeeds; FK added
                self.assertIn(_VERSION, store.migration_status()["applied"], label)
                self.assertTrue(_fk_present(store), label)
            finally:
                self._cleanup(store)


class ForeignKeyEnforcementTest(unittest.TestCase):
    def test_result_for_missing_game_is_a_translated_conflict(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                self.assertTrue(_fk_present(store), label)
                with self.assertRaises(IntegrityConflictError, msg=label) as ctx:
                    with store.transaction():
                        store.add_game_result(_result("r1", "ghost"))
                self.assertEqual(ctx.exception.details["reason"],
                                 "foreign_key_violation", label)
                # The failed write left nothing behind.
                self.assertIsNone(store.result_for_game("ghost"), label)
            finally:
                store.close()

    def test_result_for_existing_game_is_allowed(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                _add_game(store, "g1")
                with store.transaction():
                    store.add_game_result(_result("r1", "g1"))
                self.assertIsNotNone(store.result_for_game("g1"), label)
            finally:
                store.close()

    def test_null_game_id_is_still_allowed(self):
        # The foreign key is nullable, so a result not yet tied to a game is fine.
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                with store.transaction():
                    store.add_game_result(_result("r1", None))
                cur = store.conn.cursor()
                cur.execute("SELECT COUNT(*) AS n FROM game_results")
                self.assertEqual(cur.fetchone()["n"], 1, label)
            finally:
                store.close()


class RebuildPreservesDataTest(unittest.TestCase):
    """The SQLite table rebuild must carry every row and index across the swap;
    on PostgreSQL the ALTER touches nothing else. Exercised on both backends."""

    def test_rows_and_indexes_survive_the_migration(self):
        for label, url in _sql_backends():
            store = self._downgraded_with_rows(url)
            try:
                migrate(store.conn, store.dialect)  # apply 025 over real rows
                self.assertTrue(_fk_present(store), label)
                # Both rows preserved, including the NULL-game_id one.
                cur = store.conn.cursor()
                cur.execute("SELECT COUNT(*) AS n FROM game_results")
                self.assertEqual(cur.fetchone()["n"], 2, label)
                self.assertEqual(store.result_for_game("g1").home_score, 3, label)
                # The Slice 3C uniqueness index still enforces one result per game.
                with self.assertRaises(IntegrityConflictError, msg=label):
                    with store.transaction():
                        store.add_game_result(_result("r3", "g1"))
            finally:
                if store.backend != "sqlite":
                    store.reset_schema()
                store.close()

    def _downgraded_with_rows(self, url):
        store = _fresh(url)
        _downgrade_025(store)
        _add_game(store, "g1")
        with store.transaction():
            _add_result_raw(store, "r1", "g1")
            _add_result_raw(store, "r2", None)
        return store


class MigrationLoaderShapeTest(unittest.TestCase):
    """The per-dialect migration loader must fail closed: a version is either one
    portable .sql or a complete sqlite+postgres pair. A lone per-dialect file (or
    a portable/per-dialect mix) must raise, so a missing variant can never
    silently run the other engine's DDL."""

    def _load_from(self, filenames):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            for name in filenames:
                with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
                    fh.write("CREATE TABLE t (id TEXT);\n")
            original = sql_store_mod._MIGRATIONS_DIR
            sql_store_mod._MIGRATIONS_DIR = d
            try:
                return {v: s for v, s
                        in sql_store_mod._load_migrations("sqlite")}
            finally:
                sql_store_mod._MIGRATIONS_DIR = original

    def test_portable_and_complete_pair_load(self):
        loaded = self._load_from(
            ["001_a.sql", "002_b.sqlite.sql", "002_b.postgres.sql"])
        self.assertEqual(sorted(loaded), ["001_a", "002_b"])

    def test_lone_per_dialect_file_is_rejected(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._load_from(["003_c.sqlite.sql"])  # no matching .postgres.sql
        self.assertIn("003_c", str(ctx.exception))

    def test_portable_and_per_dialect_mix_is_rejected(self):
        with self.assertRaises(RuntimeError):
            self._load_from(["004_d.sql", "004_d.sqlite.sql"])


class MigrationLedgerTest(unittest.TestCase):
    def test_fk_recorded_and_enforced_after_reopen(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            first = SqlStore(path)
            self.assertIn(_VERSION, first.migration_status()["applied"])
            self.assertTrue(_fk_present(first))
            _add_game(first, "g1")
            first.close()
            second = SqlStore(path)
            self.assertTrue(second.migration_status()["current"])
            self.assertTrue(_fk_present(second))  # FK survives the restart
            with self.assertRaises(IntegrityConflictError):
                with second.transaction():
                    second.add_game_result(_result("r1", "ghost"))  # still enforced
            with second.transaction():
                second.add_game_result(_result("r2", "g1"))  # valid still works
            second.close()
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
