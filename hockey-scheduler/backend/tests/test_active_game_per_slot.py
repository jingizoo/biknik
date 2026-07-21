"""One active game per ice slot — DB-enforced invariant (#201 Slice 3A).

A partial UNIQUE index (migration 022) guarantees at most one non-cancelled
game references an ice slot, even under a cross-process race the service-level
check can't serialize. Covers: pre-migration duplicate detection/report, the
constraint on SQLite + PostgreSQL, the explicit cancelled-game rule, a real
two-connection race (PostgreSQL), the stable translated conflict for the loser
with zero partial writes, and restart/migration-ledger persistence.
"""

import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain import AuditAction, AuditLog, Game
from hockey_scheduler.domain.errors import ScheduleConflictError
from hockey_scheduler.store import SqlStore
from hockey_scheduler.store.integrity_checks import (
    MigrationDataError,
    assert_no_duplicate_active_ice_slots,
    find_duplicate_active_ice_slots,
)
from hockey_scheduler.store.sql_store import migrate

UTC = timezone.utc
_VERSION = "022_one_active_game_per_slot"
_INDEX = "ux_games_active_ice_slot"


def _game(gid, slot="slot1", cancelled=False):
    return Game(id=gid, home_team_id="h", away_team_id="a", start_time=None,
                target_goalies=1, target_skaters=4, max_skaters=6, rink="r",
                end_time=None, roster_lock_time=None, locked=False,
                cancelled=cancelled, published=False, season_id="s",
                division_id="d", ice_slot_id=slot, is_draft=False)


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


def _seed_slots(store, *slot_ids):
    """Create the ice_slots rows the planted games reference so the #201 Slice 3
    games.ice_slot_id → ice_slots(id) foreign key is satisfied. rink_id stays NULL
    (a nullable foreign key), so no rink/venue is needed; seeding the referenced
    slot does not affect the one-active-game-per-slot invariant under test."""
    with store.transaction():
        cur = store.conn.cursor()
        for sid in slot_ids:
            cur.execute(store.dialect.sql(
                "INSERT INTO ice_slots (id, rink_id) VALUES (?, ?)"), (sid, None))


def _downgrade_022(store):
    """Simulate a pre-022 database: drop the index and un-record the migration."""
    with store.transaction():
        cur = store.conn.cursor()
        cur.execute(f"DROP INDEX IF EXISTS {_INDEX}")
        cur.execute(store.dialect.sql(
            "DELETE FROM schema_migrations WHERE version = ?"), (_VERSION,))


class PreMigrationValidationTest(unittest.TestCase):
    """Existing violations are detected and reported, never guessed away."""

    def test_duplicate_active_slots_are_detected_and_named(self):
        store = SqlStore(":memory:")
        try:
            _downgrade_022(store)
            _seed_slots(store, "slotA", "slotB")
            with store.transaction():
                store.add_game(_game("g1", slot="slotA"))
                store.add_game(_game("g2", slot="slotA"))  # dup active, now allowed
                store.add_game(_game("g3", slot="slotB"))
            self.assertEqual(find_duplicate_active_ice_slots(store.conn), ["slotA"])
            with self.assertRaises(MigrationDataError) as ctx:
                assert_no_duplicate_active_ice_slots(store.conn)
            self.assertIn("slotA", str(ctx.exception))
        finally:
            store.close()

    def test_cancelled_duplicate_is_not_flagged(self):
        store = SqlStore(":memory:")
        try:
            _downgrade_022(store)
            _seed_slots(store, "slotA")
            with store.transaction():
                store.add_game(_game("g1", slot="slotA"))
                store.add_game(_game("g2", slot="slotA", cancelled=True))
            self.assertEqual(find_duplicate_active_ice_slots(store.conn), [])
            assert_no_duplicate_active_ice_slots(store.conn)  # does not raise
        finally:
            store.close()

    def test_migrate_aborts_when_existing_data_would_violate(self):
        store = SqlStore(":memory:")
        try:
            _downgrade_022(store)
            _seed_slots(store, "slotA")
            with store.transaction():
                store.add_game(_game("g1", slot="slotA"))
                store.add_game(_game("g2", slot="slotA"))
            # Re-running migrate() must refuse to add the constraint over dirty
            # data, naming the offending slot rather than an opaque index error.
            with self.assertRaises(MigrationDataError) as ctx:
                migrate(store.conn, store.dialect)
            self.assertIn("slotA", str(ctx.exception))
        finally:
            store.close()


class ConstraintEnforcementTest(unittest.TestCase):
    """The constraint holds on every SQL backend available in this environment."""

    def test_second_active_game_on_slot_is_a_translated_conflict(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                _seed_slots(store, "slot1")
                with store.transaction():
                    store.add_game(_game("g1"))
                # #201 Slice 3: the double-book loser now surfaces the domain
                # ScheduleConflictError (ice_slot_taken) — the same conflict the
                # create_game pre-check raises — not the generic unique_violation.
                with self.assertRaises(ScheduleConflictError, msg=label) as ctx:
                    with store.transaction():
                        store.add_game(_game("g2"))
                self.assertEqual(ctx.exception.details["reason"],
                                 "ice_slot_taken", label)
                self.assertEqual(ctx.exception.details["ice_slot_id"],
                                 "slot1", label)
            finally:
                store.close()

    def test_cancelled_game_releases_the_slot(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                _seed_slots(store, "slot1")
                with store.transaction():
                    store.add_game(_game("g1"))
                game = store.get_game("g1")
                game.cancelled = True
                with store.transaction():
                    store.save_game(game)
                # A cancelled game keeps its slot reference but no longer
                # reserves it, so a new active game may take the slot.
                with store.transaction():
                    store.add_game(_game("g2"))
                self.assertIsNotNone(store.get_game("g2"), label)
            finally:
                store.close()

    def test_conflict_makes_zero_game_or_audit_writes(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                _seed_slots(store, "slot1")
                with store.transaction():
                    store.add_game(_game("g1"))
                with self.assertRaises(ScheduleConflictError, msg=label):
                    with store.transaction():
                        store.add_audit(AuditLog(
                            id="a1", game_id="g9",
                            action=AuditAction.ROSTER_SELECTED,
                            at=datetime(2026, 1, 1, tzinfo=UTC)))
                        store.add_game(_game("g2"))  # violates → whole txn rolls back
                self.assertIsNone(store.get_game("g2"), label)
                self.assertEqual(store.audit_for_game("g9"), [], label)
            finally:
                store.close()


class SlotRaceTest(unittest.TestCase):
    """A genuine two-connection race: exactly one game wins the slot."""

    @unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                         "cross-connection race needs PostgreSQL")
    def test_only_one_of_two_racing_inserts_wins(self):
        url = os.environ["TEST_DATABASE_URL"]
        seed = SqlStore(url)
        seed.reset_schema()
        _seed_slots(seed, "race_slot")   # satisfy games.ice_slot_id → ice_slots
        seed.close()
        barrier = threading.Barrier(2)
        results = {}

        def attempt(gid):
            store = SqlStore(url)
            try:
                barrier.wait(timeout=5)
                with store.transaction():
                    store.add_game(_game(gid, slot="race_slot"))
                results[gid] = "won"
            except ScheduleConflictError:
                results[gid] = "conflict"
            except Exception as exc:  # pragma: no cover - surfaced by assert
                results[gid] = f"error:{exc!r}"
            finally:
                store.close()

        threads = [threading.Thread(target=attempt, args=(g,))
                   for g in ("game_a", "game_b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(sorted(results.values()), ["conflict", "won"], results)
        checker = SqlStore(url)
        try:
            cur = checker.conn.cursor()
            cur.execute(checker.dialect.sql(
                "SELECT COUNT(*) AS n FROM games "
                "WHERE ice_slot_id = ? AND cancelled = 0"), ("race_slot",))
            self.assertEqual(cur.fetchone()["n"], 1)  # loser wrote nothing
        finally:
            checker.close()


class MigrationLedgerTest(unittest.TestCase):
    """The constraint is recorded, idempotent, and survives a restart."""

    def test_recorded_and_enforced_after_reopen(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            first = SqlStore(path)
            self.assertIn(_VERSION, first.migration_status()["applied"])
            _seed_slots(first, "slot1")
            with first.transaction():
                first.add_game(_game("g1"))
            first.close()
            # Reopen: migrate() is idempotent, and the index still enforces.
            second = SqlStore(path)
            self.assertIn(_VERSION, second.migration_status()["applied"])
            self.assertTrue(second.migration_status()["current"])
            with self.assertRaises(ScheduleConflictError):
                with second.transaction():
                    second.add_game(_game("g2"))
            second.close()
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
