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


class PreMigrationValidationTest(unittest.TestCase):
    def test_duplicate_roster_rows_are_detected_and_named(self):
        store = SqlStore(":memory:")
        try:
            _downgrade_023(store)
            with store.transaction():
                store.add_roster_entry(_entry("e1", "g1", "p1"))
                store.add_roster_entry(_entry("e2", "g1", "p1"))  # dup, now allowed
                store.add_roster_entry(_entry("e3", "g1", "p2"))
            self.assertEqual(find_duplicate_roster_players(store.conn),
                             [("g1", "p1")])
            with self.assertRaises(MigrationDataError) as ctx:
                assert_no_duplicate_roster_players(store.conn)
            self.assertIn("g1/p1", str(ctx.exception))
        finally:
            store.close()

    def test_migrate_aborts_when_existing_data_would_violate(self):
        store = SqlStore(":memory:")
        try:
            _downgrade_023(store)
            with store.transaction():
                store.add_roster_entry(_entry("e1", "g1", "p1"))
                store.add_roster_entry(_entry("e2", "g1", "p1"))
            with self.assertRaises(MigrationDataError) as ctx:
                migrate(store.conn, store.dialect)
            self.assertIn("g1/p1", str(ctx.exception))
        finally:
            store.close()


class ConstraintEnforcementTest(unittest.TestCase):
    def test_second_row_for_same_pair_is_a_translated_conflict(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
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

    def test_conflict_makes_zero_writes(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
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
        SqlStore(url).reset_schema()
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


class MigrationLedgerTest(unittest.TestCase):
    def test_recorded_and_enforced_after_reopen(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            first = SqlStore(path)
            self.assertIn(_VERSION, first.migration_status()["applied"])
            with first.transaction():
                first.add_roster_entry(_entry("e1"))
            first.close()
            second = SqlStore(path)
            self.assertTrue(second.migration_status()["current"])
            with self.assertRaises(IntegrityConflictError):
                with second.transaction():
                    second.add_roster_entry(_entry("e2"))  # still enforced
            second.close()
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
