"""One roster row per (game, player) — DB-enforced invariant (#201 Slice 3B).

A UNIQUE index (migration 023) guarantees a game/player pair has at most one
roster row, so the service's revive-don't-duplicate rule (select_roster) holds
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

from hockey_scheduler.domain import (
    GameRosterEntry,
    RosterEntryStatus,
    RosterRole,
    SelectionSource,
)
from hockey_scheduler.domain.errors import IntegrityConflictError
from hockey_scheduler.store import SqlStore
from hockey_scheduler.store.integrity_checks import (
    MigrationDataError,
    assert_no_duplicate_roster_players,
    find_duplicate_roster_players,
)
from hockey_scheduler.store.sql_store import migrate

UTC = timezone.utc
_VERSION = "023_one_roster_row_per_player"
_INDEX = "ux_roster_game_player"


def _entry(eid, game="g1", player="p1"):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return GameRosterEntry(
        id=eid, game_id=game, player_id=player, roster_role=RosterRole.SELECTED,
        selection_source=SelectionSource.COACH_SELECTED,
        status=RosterEntryStatus.SELECTED, selected_at=now, updated_at=now,
        selected_by="actor")


def _ensure_parents(store, games=(), players=()):
    """Seed the parent games/players a roster entry references (migration 027 adds
    game_roster_entries.game_id → games and .player_id → players foreign keys, so
    a concrete reference must name a real row). NULL references need no parent."""
    cur = store.conn.cursor()
    for table, ids in (("games", games), ("players", players)):
        for rid in ids:
            if rid is None:
                continue
            cur.execute(store.dialect.sql(
                f"SELECT 1 FROM {table} WHERE id = ?"), (rid,))
            if cur.fetchone() is None:
                cur.execute(store.dialect.sql(
                    f"INSERT INTO {table} (id) VALUES (?)"), (rid,))


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


def _downgrade_023(store):
    """Simulate a pre-023 database: drop the unique index and un-record it."""
    with store.transaction():
        cur = store.conn.cursor()
        cur.execute(f"DROP INDEX IF EXISTS {_INDEX}")
        cur.execute(store.dialect.sql(
            "DELETE FROM schema_migrations WHERE version = ?"), (_VERSION,))


def _game_lookup_index_present(store):
    """True if a NON-partial ix_roster_game (usable for roster_for_game's
    WHERE game_id = ? scan, including NULL-player rows) exists."""
    cur = store.conn.cursor()
    if store.backend == "sqlite":
        cur.execute("PRAGMA index_list('game_roster_entries')")
        return any(row["name"] == "ix_roster_game" and row["partial"] == 0
                   for row in cur.fetchall())
    cur.execute("SELECT indexname, indexdef FROM pg_indexes "
                "WHERE tablename = 'game_roster_entries'")
    return any(row["indexname"] == "ix_roster_game"
               and "WHERE" not in row["indexdef"].upper()
               for row in cur.fetchall())


def _unique_partial_index_present(store):
    """True if the PARTIAL unique ux_roster_game_player (concrete-pair
    uniqueness, NULL-bearing rows excluded) exists."""
    cur = store.conn.cursor()
    if store.backend == "sqlite":
        cur.execute("PRAGMA index_list('game_roster_entries')")
        return any(row["name"] == _INDEX and row["unique"] == 1
                   and row["partial"] == 1 for row in cur.fetchall())
    cur.execute("SELECT indexname, indexdef FROM pg_indexes "
                "WHERE tablename = 'game_roster_entries'")
    return any(row["indexname"] == _INDEX
               and "UNIQUE" in row["indexdef"].upper()
               and "WHERE" in row["indexdef"].upper()
               for row in cur.fetchall())


class PreMigrationValidationTest(unittest.TestCase):
    """The pre-enable check matches the partial index exactly, on each backend.

    Parametrized over SQLite + PostgreSQL (in CI) so the NULL semantics — which
    differ from a naive GROUP BY — are proven on both, not just SQLite.
    """

    def _pre023(self, url):
        store = _fresh(url)
        _downgrade_023(store)
        return store

    def _cleanup(self, store):
        # These tests write duplicate rows a shared PostgreSQL DB would carry
        # into later suites (whose SqlStore() re-runs migrate() → the 023
        # pre-check would then abort on our leftovers). Reset to a clean,
        # fully-migrated schema before closing so the shared DB is left pristine.
        if store.backend != "sqlite":
            store.reset_schema()
        store.close()

    def test_concrete_duplicates_detected_and_migrate_aborts(self):
        for label, url in _sql_backends():
            store = self._pre023(url)
            try:
                _ensure_parents(store, ["g1"], ["p1", "p2"])
                with store.transaction():
                    store.add_roster_entry(_entry("e1", "g1", "p1"))
                    store.add_roster_entry(_entry("e2", "g1", "p1"))  # dup
                    store.add_roster_entry(_entry("e3", "g1", "p2"))
                self.assertEqual(find_duplicate_roster_players(store.conn),
                                 [("g1", "p1")], label)
                with self.assertRaises(MigrationDataError, msg=label) as ctx:
                    assert_no_duplicate_roster_players(store.conn)
                self.assertIn("g1/p1", str(ctx.exception))
                with self.assertRaises(MigrationDataError, msg=label):
                    migrate(store.conn, store.dialect)  # aborts on dirty data
            finally:
                self._cleanup(store)

    def test_null_bearing_duplicates_are_not_flagged_and_do_not_crash(self):
        # game_id/player_id are nullable and NULLs are distinct in the partial
        # index, so repeated NULL-bearing rows are NOT duplicates. The check must
        # agree (no false positive) and must not raise ordering the tuples.
        for label, url in _sql_backends():
            store = self._pre023(url)
            try:
                _ensure_parents(store, ["g1"], ["p1"])
                with store.transaction():
                    store.add_roster_entry(_entry("e1", None, "p1"))
                    store.add_roster_entry(_entry("e2", None, "p1"))
                    store.add_roster_entry(_entry("e3", "g1", None))
                    store.add_roster_entry(_entry("e4", "g1", None))
                self.assertEqual(find_duplicate_roster_players(store.conn), [], label)
                assert_no_duplicate_roster_players(store.conn)  # must not raise
                migrate(store.conn, store.dialect)  # succeeds; index is created
                self.assertIn(_VERSION, store.migration_status()["applied"], label)
            finally:
                self._cleanup(store)

    def test_mixed_null_and_concrete_group_reports_only_the_concrete_pair(self):
        # A dirty mix — (NULL,'p1') alongside two ('g1','p1') — must report only
        # the concrete duplicate and never raise a TypeError ordering None vs str.
        for label, url in _sql_backends():
            store = self._pre023(url)
            try:
                _ensure_parents(store, ["g1"], ["p1"])
                with store.transaction():
                    store.add_roster_entry(_entry("e1", None, "p1"))
                    store.add_roster_entry(_entry("e2", "g1", "p1"))
                    store.add_roster_entry(_entry("e3", "g1", "p1"))
                self.assertEqual(find_duplicate_roster_players(store.conn),
                                 [("g1", "p1")], label)
                with self.assertRaises(MigrationDataError, msg=label):
                    assert_no_duplicate_roster_players(store.conn)
            finally:
                self._cleanup(store)


class ConstraintEnforcementTest(unittest.TestCase):
    def test_second_row_for_same_pair_is_a_translated_conflict(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                _ensure_parents(store, ["g1"], ["p1", "p2"])
                with store.transaction():
                    store.add_roster_entry(_entry("e1"))
                with self.assertRaises(IntegrityConflictError, msg=label) as ctx:
                    with store.transaction():
                        store.add_roster_entry(_entry("e2"))  # same (g1, p1)
                self.assertEqual(ctx.exception.details["reason"],
                                 "unique_violation", label)
                # A different player on the same game is unaffected.
                with store.transaction():
                    store.add_roster_entry(_entry("e3", player="p2"))
                self.assertIsNotNone(store.roster_entry_for_player("g1", "p2"), label)
            finally:
                store.close()

    def test_revive_path_still_works_under_the_constraint(self):
        # The service revives a removed row rather than inserting a duplicate;
        # the constraint must not break that (save_roster_entry updates in place).
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                _ensure_parents(store, ["g1"], ["p1"])
                with store.transaction():
                    store.add_roster_entry(_entry("e1"))
                entry = store.roster_entry_for_player("g1", "p1")
                entry.status = RosterEntryStatus.REMOVED
                with store.transaction():
                    store.save_roster_entry(entry)
                entry.status = RosterEntryStatus.SELECTED  # revive
                with store.transaction():
                    store.save_roster_entry(entry)
                rows = [e for e in store.roster_for_game("g1") if e.player_id == "p1"]
                self.assertEqual(len(rows), 1, label)  # still exactly one row
            finally:
                store.close()

    def test_null_bearing_rows_are_allowed_by_the_partial_index(self):
        # The index only constrains concrete (game_id, player_id) pairs, so
        # NULL-bearing rows never conflict (matching the pre-migration check).
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                _ensure_parents(store, ["g1"], ["p1"])
                with store.transaction():  # none of these raise a conflict
                    store.add_roster_entry(_entry("e1", None, "p1"))
                    store.add_roster_entry(_entry("e2", None, "p1"))
                    store.add_roster_entry(_entry("e3", "g1", None))
                    store.add_roster_entry(_entry("e4", "g1", None))
                cur = store.conn.cursor()
                cur.execute("SELECT COUNT(*) AS n FROM game_roster_entries")
                self.assertEqual(cur.fetchone()["n"], 4, label)
            finally:
                store.close()

    def test_conflict_makes_zero_writes(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                _ensure_parents(store, ["g1"], ["p1", "p9"])
                with store.transaction():
                    store.add_roster_entry(_entry("e1"))
                with self.assertRaises(IntegrityConflictError, msg=label):
                    with store.transaction():
                        store.add_roster_entry(_entry("e3", player="p9"))  # ok write
                        store.add_roster_entry(_entry("e2"))  # violates → rollback
                self.assertIsNone(store.roster_entry_for_player("g1", "p9"), label)
            finally:
                store.close()


class RosterRaceTest(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                         "cross-connection race needs PostgreSQL")
    def test_only_one_of_two_racing_inserts_wins(self):
        url = os.environ["TEST_DATABASE_URL"]
        seed = SqlStore(url)
        seed.reset_schema()
        _ensure_parents(seed, ["race_g"], ["race_p"])  # FK parents for the race
        seed.close()
        barrier = threading.Barrier(2)
        results = {}

        def attempt(eid):
            store = SqlStore(url)
            try:
                barrier.wait(timeout=5)
                with store.transaction():
                    store.add_roster_entry(_entry(eid, "race_g", "race_p"))
                results[eid] = "won"
            except IntegrityConflictError:
                results[eid] = "conflict"
            except Exception as exc:  # pragma: no cover
                results[eid] = f"error:{exc!r}"
            finally:
                store.close()

        threads = [threading.Thread(target=attempt, args=(e,))
                   for e in ("entry_a", "entry_b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(sorted(results.values()), ["conflict", "won"], results)
        checker = SqlStore(url)
        try:
            cur = checker.conn.cursor()
            cur.execute(checker.dialect.sql(
                "SELECT COUNT(*) AS n FROM game_roster_entries "
                "WHERE game_id = ? AND player_id = ?"), ("race_g", "race_p"))
            self.assertEqual(cur.fetchone()["n"], 1)
        finally:
            checker.close()


class IndexShapeTest(unittest.TestCase):
    """After migration 023 both indexes must coexist on every backend: the
    PARTIAL unique index that enforces one row per concrete pair, and a
    NON-partial ix_roster_game that still serves roster_for_game's
    ``WHERE game_id = ?`` scan (including NULL-player rows the partial index
    excludes). Dropping the latter for the former would silently full-scan.
    """

    def test_partial_unique_and_nonpartial_lookup_indexes_coexist(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                _ensure_parents(store, ["g1"], ["p1"])
                # A NULL-player row lives outside the partial unique index; the
                # non-partial lookup index is what keeps its per-game read fast.
                with store.transaction():
                    store.add_roster_entry(_entry("e1", "g1", None))
                self.assertTrue(_game_lookup_index_present(store),
                                f"{label}: non-partial ix_roster_game missing")
                self.assertTrue(_unique_partial_index_present(store),
                                f"{label}: partial ux_roster_game_player missing")
                # The lookup row is readable and the partial index still enforces
                # concrete uniqueness alongside it.
                self.assertEqual(len(store.roster_for_game("g1")), 1, label)
                with store.transaction():
                    store.add_roster_entry(_entry("e2", "g1", "p1"))
                with self.assertRaises(IntegrityConflictError, msg=label):
                    with store.transaction():
                        store.add_roster_entry(_entry("e3", "g1", "p1"))
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
            _ensure_parents(first, ["g1"], ["p1"])
            with first.transaction():
                first.add_roster_entry(_entry("e1"))
                first.add_roster_entry(_entry("e_null", "g1", None))  # NULL-player
            first.close()
            second = SqlStore(path)
            self.assertTrue(second.migration_status()["current"])
            # Both index shapes survive the restart, not just the unique one.
            self.assertTrue(_game_lookup_index_present(second))
            self.assertTrue(_unique_partial_index_present(second))
            self.assertEqual(len(second.roster_for_game("g1")), 2)  # NULL row kept
            with self.assertRaises(IntegrityConflictError):
                with second.transaction():
                    second.add_roster_entry(_entry("e2"))  # still enforced
            second.close()
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
