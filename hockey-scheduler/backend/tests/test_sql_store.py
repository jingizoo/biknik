"""SQL store tests.

Run the real service/API flows against the SqlStore (SQLite adapter) to prove
parity with the in-memory store, plus reload tests that re-open the database and
confirm state persisted. The same SqlStore runs against PostgreSQL in CI via
DATABASE_URL.
"""

import os
import tempfile
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.full_demo import build_full_demo_store
from hockey_scheduler.store import SqlStore, create_store


class SqlStoreParityTest(unittest.TestCase):
    """The full demo + E2E substitute flow must work on the SQL store."""

    def setUp(self):
        self.store = SqlStore(":memory:")
        _, self.game_id, self.ids = build_full_demo_store(self.store)
        self.api = ApiService(self.store)

    def test_seed_and_overview(self):
        ov = self.api.get_demo_overview()
        self.assertEqual(ov["league"]["name"], "Alpine Ice Hockey League")
        self.assertEqual({t["name"] for t in ov["teams"]},
                         {"U16 Lions", "U16 Falcons", "U18 Lions", "Senior Lions"})
        self.assertEqual(len(ov["public_fixtures"]), 1)  # seeded game published

    def test_opens_confirmed(self):
        self.assertEqual(self.api.get_roster_status(self.game_id)["status"],
                         "roster_confirmed")

    def test_end_to_end_flow_on_sql(self):
        gid = self.game_id
        self.api.set_availability(gid, self.ids["selected_player_id"], "unavailable")
        self.assertEqual(self.api.get_roster_status(gid)["status"], "open_slot")
        self.api.enroll_substitute(gid, self.ids["substitute_player_id"])
        self.assertEqual(self.api.get_roster_status(gid)["status"], "needs_substitute")
        self.api.add_substitute_to_roster(gid, self.ids["substitute_player_id"],
                                          actor_id="coach")
        self.assertEqual(self.api.get_roster_status(gid)["status"], "roster_confirmed")
        self.api.lock_roster(gid, actor_id="coach")
        self.assertEqual(self.api.get_roster_status(gid)["status"], "locked")

    def test_scheduling_rules_enforced_on_sql(self):
        ov = self.api.get_demo_overview()
        slot = next(s for s in ov["ice_slots"] if s["status"] == "available")
        u16 = [t for t in ov["teams"] if t["division_name"] == "U16 Elite"]
        # schedule a draft game, then double-book the slot → conflict
        self.api.create_game(ov["seasons"][0]["id"], u16[0]["division_id"],
                             u16[0]["id"], u16[1]["id"], slot["id"])
        dup = self.api.create_game(ov["seasons"][0]["id"], u16[0]["division_id"],
                                   u16[0]["id"], u16[1]["id"], slot["id"])
        self.assertEqual(dup["error"]["code"], "schedule_conflict")


class SqlStoreReloadTest(unittest.TestCase):
    """State must survive re-opening the database (the whole point of #23)."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        os.remove(self.path)

    def _api(self):
        return ApiService(SqlStore(self.path))

    def test_state_persists_across_reload(self):
        store = SqlStore(self.path)
        _, gid, ids = build_full_demo_store(store)
        api = ApiService(store)
        # Mutate: back out, add substitute, lock — and schedule + publish a 2nd game.
        api.set_availability(gid, ids["selected_player_id"], "unavailable")
        api.enroll_substitute(gid, ids["substitute_player_id"])
        api.add_substitute_to_roster(gid, ids["substitute_player_id"], actor_id="c")
        api.lock_roster(gid, actor_id="c")
        ov = api.get_demo_overview()
        slot = next(s for s in ov["ice_slots"] if s["status"] == "available")
        u16 = [t for t in ov["teams"] if t["division_name"] == "U16 Elite"]
        g2 = api.create_game(ov["seasons"][0]["id"], u16[0]["division_id"],
                             u16[0]["id"], u16[1]["id"], slot["id"])
        api.publish_game(g2["id"], actor_id="c")
        used_slot = slot["id"]

        # Re-open the database in a fresh store/connection.
        api2 = self._api()
        # roster lock persisted
        self.assertEqual(api2.get_roster_status(gid)["status"], "locked")
        ov2 = api2.get_demo_overview()
        # the second game persisted and is published (in public fixtures)
        ids2 = {g["game_id"] for g in ov2["schedule"]}
        self.assertIn(g2["id"], ids2)
        self.assertEqual(len(ov2["public_fixtures"]), 2)
        # its ice slot is now allocated (allocation persisted)
        slot2 = next(s for s in ov2["ice_slots"] if s["id"] == used_slot)
        self.assertEqual(slot2["status"], "allocated")
        # league/season/divisions/teams persisted
        self.assertEqual(ov2["league"]["name"], "Alpine Ice Hockey League")
        self.assertEqual(len(ov2["teams"]), 4)

    def test_create_store_factory_selects_sql_for_url(self):
        store = create_store(self.path)
        self.assertIsInstance(store, SqlStore)


if __name__ == "__main__":
    unittest.main()
