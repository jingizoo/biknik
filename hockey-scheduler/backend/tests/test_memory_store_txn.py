"""In-memory store transaction semantics (#201 Slice 1).

The in-memory store is the default/demo store and the atomicity reference for
most service tests, so it must honor the same transaction contract as the SQL
store: a failure mid multi-write operation rolls the whole thing back, leaving
zero partial state — not merely holding a lock. These tests mirror
``SqlStoreTransactionTest`` and add the contract cases specific to snapshot
rollback (in-place mutation, id-counter, reentrant nesting).
"""

import unittest
from datetime import datetime, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.services import SetupService
from hockey_scheduler.store import InMemoryStore

UTC = timezone.utc


def _seed_scheduleable(store):
    """A league/season/division with two registered teams and a free slot."""
    svc = SetupService(store)
    league = svc.create_league("L")
    season = svc.create_season(league.id, "S")
    div = svc.create_division(season.id, "D")
    home = svc.create_team(svc.create_club("CA").id, div.id, "TA")
    away = svc.create_team(svc.create_club("CB").id, div.id, "TB")
    svc.register_team_for_season(season.id, home.id, div.id)
    svc.register_team_for_season(season.id, away.id, div.id)
    rink = svc.create_rink(svc.create_venue("V", league_id=league.id).id, "R")
    slot = svc.create_ice_slot(rink.id, datetime(2026, 9, 1, 18, 30, tzinfo=UTC),
                               datetime(2026, 9, 1, 20, 0, tzinfo=UTC))
    return svc, season, div, home, away, slot


class MemoryStoreTransactionTest(unittest.TestCase):
    """A failure mid multi-write operation must roll the whole thing back."""

    def test_failed_create_game_rolls_back(self):
        # Mirror of SqlStoreTransactionTest.test_failed_create_game_rolls_back.
        store = InMemoryStore()
        svc, season, div, home, away, slot = _seed_scheduleable(store)
        self.assertEqual(len(store.all_games()), 0)

        # Force the final step of create_game (the audit write) to fail, after
        # the game insert and the slot-allocation update.
        def boom(*_a, **_k):
            raise RuntimeError("audit write failed")
        store.add_setup_audit = boom
        with self.assertRaises(RuntimeError):
            svc.create_game(season.id, div.id, home.id, away.id, slot.id)

        # The transaction rolled back: no game, and the slot is still available.
        self.assertEqual(len(store.all_games()), 0)
        self.assertEqual(store.get_ice_slot(slot.id).status.value, "available")

    def test_rollback_undoes_in_place_field_mutation(self):
        # A snapshot must deep-copy, not shallow-copy: services mutate the very
        # dataclass the store holds, so a shallow dict copy would not undo an
        # in-place field change on rollback.
        store = InMemoryStore()
        svc, season, div, home, away, slot = _seed_scheduleable(store)
        game = svc.create_game(season.id, div.id, home.id, away.id, slot.id)
        self.assertFalse(store.get_game(game.id).published)

        with self.assertRaises(RuntimeError):
            with store.transaction():
                store.get_game(game.id).published = True  # in-place mutation
                raise RuntimeError("abort")
        self.assertFalse(store.get_game(game.id).published)

    def test_rollback_undoes_wholesale_dict_field_replacement(self):
        # The snapshot invariant is that nested dict fields (audit detail,
        # account scope) are replaced wholesale, not mutated in place. Prove the
        # supported pattern rolls back: reassigning such a field to a new dict is
        # undone on rollback.
        from hockey_scheduler.domain import UserAccount
        store = InMemoryStore()
        acct = UserAccount(id=store.next_id("user"), username="u",
                           password_hash="h", role="viewer",
                           created_at=datetime(2026, 1, 1, tzinfo=UTC),
                           scope={"team_id": "t1"})
        store.user_accounts[acct.id] = acct
        with self.assertRaises(RuntimeError):
            with store.transaction():
                store.user_accounts[acct.id].scope = {"team_id": "t2"}  # wholesale
                raise RuntimeError("abort")
        self.assertEqual(store.user_accounts[acct.id].scope, {"team_id": "t1"})

    def test_rollback_restores_id_counter(self):
        # Ids consumed inside a rolled-back transaction are handed back, so the
        # next successful write reuses them (parity with the SQL counters table,
        # which rolls back with its transaction).
        store = InMemoryStore()
        first = store.next_id("thing")
        with self.assertRaises(RuntimeError):
            with store.transaction():
                store.next_id("thing")
                store.next_id("thing")
                raise RuntimeError("abort")
        # The two ids burned inside the aborted txn are reclaimed.
        self.assertEqual(store.next_id("thing"),
                         f"thing_{int(first.split('_')[1]) + 1}")

    def test_id_counters_are_copy_safe_plain_ints(self):
        # Regression: id counters were itertools.count objects, which are not
        # copyable on Python 3.14 (copy.copy raises "cannot pickle
        # 'itertools.count'"), so the transaction snapshot crashed every
        # in-memory write in production while CI on an older Python stayed green.
        # Counters must be plain ints, and a snapshot of a populated store must
        # not raise on any supported version.
        store = InMemoryStore()
        store.next_id("thing")
        store.next_id("thing")
        self.assertTrue(all(isinstance(v, int) for v in store._counters.values()))
        store._snapshot()  # must not raise
        with store.transaction():  # the full write path must not raise either
            store.add_team(_bare_team(store, "t"))

    def test_next_id_is_unique_under_concurrency(self):
        # next_id is a read-modify-write (no longer itertools.count's atomic
        # next()) and may be called outside a transaction on the threaded
        # server, so concurrent callers must never receive a duplicate id.
        import threading
        store = InMemoryStore()
        threads_n, per_thread = 8, 200
        ids = []
        ids_lock = threading.Lock()
        barrier = threading.Barrier(threads_n)

        def worker():
            barrier.wait(timeout=5)  # maximize overlap on the increment
            local = [store.next_id("thing") for _ in range(per_thread)]
            with ids_lock:
                ids.extend(local)

        workers = [threading.Thread(target=worker) for _ in range(threads_n)]
        for t in workers:
            t.start()
        for t in workers:
            t.join(timeout=10)

        total = threads_n * per_thread
        self.assertEqual(len(ids), total)
        self.assertEqual(len(set(ids)), total, "duplicate ids handed out")
        self.assertEqual(store._counters["thing"], total)
        # The sequence continues correctly after the concurrent burst.
        self.assertEqual(store.next_id("thing"), f"thing_{total + 1}")

    def test_nested_transaction_joins_the_outer_unit(self):
        # A nested transaction does not open a second unit: if the OUTER body
        # fails after an inner transaction "completed", the inner writes roll
        # back too.
        store = InMemoryStore()
        svc, season, div, home, away, slot = _seed_scheduleable(store)
        teams_before = len(store.all_teams())

        with self.assertRaises(RuntimeError):
            with store.transaction():
                with store.transaction():
                    store.add_team(_bare_team(store, "inner"))
                self.assertEqual(len(store.all_teams()), teams_before + 1)
                raise RuntimeError("outer abort")
        self.assertEqual(len(store.all_teams()), teams_before)

    def test_successful_transaction_commits(self):
        store = InMemoryStore()
        svc, season, div, home, away, slot = _seed_scheduleable(store)
        game = svc.create_game(season.id, div.id, home.id, away.id, slot.id)
        self.assertEqual(len(store.all_games()), 1)
        self.assertEqual(store.get_ice_slot(slot.id).status.value, "allocated")
        self.assertIsNotNone(store.get_game(game.id))


def _bare_team(store, name):
    from hockey_scheduler.domain import Team
    return Team(id=store.next_id("team"), name=name, league_id=None, club_id=None)


if __name__ == "__main__":
    unittest.main()
